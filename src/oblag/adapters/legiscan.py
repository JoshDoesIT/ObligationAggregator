from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date

from oblag.adapters import register
from oblag.adapters.base import (
    FetchContext,
    NormalizedDate,
    NormalizedItem,
    RawDocument,
    SourceAdapter,
)
from oblag.config import get_settings
from oblag.db.models import Confidence, DateType

BASE = "https://api.legiscan.com/"

# LegiScan bill status codes. Scope boundary (spec 00): introduced/engrossed bills are
# weak signals — only enrolled/passed (adopted or about to be) and vetoed enter the
# pipeline. Vetoed matters because it terminates an item we may already track.
STATUS_MAP = {
    3: "enrolled",
    4: "passed",
    5: "vetoed",
}
MAX_BILL_FETCHES = 25  # per run, keeps within the 30k/month free tier at daily cadence


@register
class LegiscanAdapter(SourceAdapter):
    """US state legislation via LegiScan getSearchRaw + getBill (key required).

    Tracks adopted-but-not-yet-effective state laws matching the configured query
    (default: comprehensive privacy). change_hash gates getBill calls so unchanged
    bills cost one search hit, not a detail fetch."""

    name = "legiscan"
    jurisdiction = "US-States"

    def enabled(self) -> bool:
        settings = get_settings()
        return bool(settings.legiscan_api_key and settings.legiscan_states)

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        settings = get_settings()
        key = settings.legiscan_api_key or ""
        states = [s.strip() for s in (settings.legiscan_states or "").split(",") if s.strip()]
        query = settings.legiscan_query
        known_hashes: dict[str, str] = ctx.params.get("known_hashes", {})

        detail_budget = MAX_BILL_FETCHES
        for state in states:
            resp = ctx.client.get(
                BASE,
                params={"key": key, "op": "getSearchRaw", "state": state, "query": query},
            )
            resp.raise_for_status()
            yield RawDocument(
                url=str(resp.url).replace(key, "***"),
                content=resp.content,
                http_status=resp.status_code,
                http_headers=dict(resp.headers),
                meta={"kind": "search", "state": state},
            )
            try:
                body = resp.json()
            except json.JSONDecodeError:
                continue
            results = (body.get("searchresult") or {}).get("results") or []
            for hit in results:
                bill_id = hit.get("bill_id")
                change_hash = hit.get("change_hash") or ""
                if not bill_id or detail_budget <= 0:
                    continue
                if known_hashes.get(str(bill_id)) == change_hash:
                    continue  # unchanged since last run
                detail = ctx.client.get(
                    BASE, params={"key": key, "op": "getBill", "id": str(bill_id)}
                )
                if detail.status_code != 200:
                    continue
                detail_budget -= 1
                yield RawDocument(
                    url=str(detail.url).replace(key, "***"),
                    content=detail.content,
                    http_status=detail.status_code,
                    http_headers=dict(detail.headers),
                    meta={"kind": "bill"},
                )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        if raw.meta.get("kind") != "bill":
            return
        try:
            body = json.loads(raw.content)
        except json.JSONDecodeError:
            return
        bill = body.get("bill") or {}
        item = self._normalize_bill(bill)
        if item is not None:
            yield item

    def _normalize_bill(self, bill: dict) -> NormalizedItem | None:
        bill_id = bill.get("bill_id")
        status = bill.get("status")
        if not bill_id or status not in STATUS_MAP:
            return None  # weak signal or malformed — out of scope
        state = bill.get("state") or "??"
        number = bill.get("bill_number") or str(bill_id)
        native = STATUS_MAP[status]

        dates: list[NormalizedDate] = []
        status_date = _parse_date(bill.get("status_date"))
        if status_date and native in ("enrolled", "passed"):
            dates.append(NormalizedDate(DateType.adopted, status_date, Confidence.published_firm))
        # LegiScan carries no reliable effective date — those arrive via curated
        # assert-date with a citation (spec: Unified-Agenda-style workflow).

        return NormalizedItem(
            source_system=self.name,
            external_key=("legiscan_bill", str(bill_id)),
            jurisdiction=f"US-{state}",
            title=f"{state} {number}: {bill.get('title') or ''}".strip(": "),
            abstract=bill.get("description"),
            url=bill.get("url") or bill.get("state_link"),
            native_status=native,
            track="final",
            join_keys=[("bill_id", f"{state}-{number}")],
            dates=dates,
            native_meta={"change_hash": bill.get("change_hash") or "", "state": state},
        )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
