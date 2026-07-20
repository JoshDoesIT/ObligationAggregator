from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

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

BASE = "https://api.regulations.gov/v4"
PAGE_SIZE = 250
MAX_PAGES = 20  # v4 hard cap: 5,000 records per query
_EASTERN = ZoneInfo("America/New_York")

# Sentinel strings the API returns instead of null (observed live: CIRCIA docket
# rin == "Not Assigned"). Joining on these would collapse unrelated dockets.
_RIN_SENTINELS = {"", "not assigned", "n/a", "none", "pending"}

TYPE_TO_NATIVE = {"Proposed Rule": "PRORULE", "Rule": "RULE"}
NATIVE_TO_TRACK = {"PRORULE": "proposed", "RULE": "final"}


@register
class RegulationsGovAdapter(SourceAdapter):
    """Enrichment adapter: joins onto Federal Register items via frDocNum/docket/RIN,
    adding docket metadata; also primary source for regs.gov-only rulemakings."""

    name = "regulations_gov"
    jurisdiction = "US-Federal"

    def enabled(self) -> bool:
        return bool(get_settings().regsgov_api_key)

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": get_settings().regsgov_api_key or ""}

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        pages: list[RawDocument] = []
        docket_ids: set[str] = set()
        for doc_type in ("Proposed Rule", "Rule"):
            for page_num in range(1, MAX_PAGES + 1):
                params: dict[str, str] = {
                    "filter[documentType]": doc_type,
                    "page[size]": str(PAGE_SIZE),
                    "page[number]": str(page_num),
                    "sort": "lastModifiedDate",
                }
                if ctx.since:
                    params["filter[lastModifiedDate][ge]"] = ctx.since.astimezone(
                        _EASTERN
                    ).strftime("%Y-%m-%d %H:%M:%S")
                elif ctx.window:
                    params["filter[postedDate][ge]"] = ctx.window[0].isoformat()
                    params["filter[postedDate][le]"] = ctx.window[1].isoformat()
                resp = ctx.client.get(f"{BASE}/documents", params=params, headers=self._headers())
                resp.raise_for_status()
                body = resp.json()
                raw = RawDocument(
                    url=str(resp.url),
                    content=resp.content,
                    http_status=resp.status_code,
                    http_headers=dict(resp.headers),
                )
                pages.append(raw)
                for doc in body.get("data") or []:
                    docket = (doc.get("attributes") or {}).get("docketId")
                    if docket:
                        docket_ids.add(docket)
                if not (body.get("meta") or {}).get("hasNextPage"):
                    break

        # docket enrichment: v4 returns RIN only on /dockets/{id}
        docket_info: dict[str, dict] = {}
        for docket_id in sorted(docket_ids):
            resp = ctx.client.get(f"{BASE}/dockets/{docket_id}", headers=self._headers())
            if resp.status_code != 200:
                continue
            attrs = (resp.json().get("data") or {}).get("attributes") or {}
            docket_info[docket_id] = {
                "rin": attrs.get("rin"),
                "docketType": attrs.get("docketType"),
                "title": attrs.get("title"),
            }
            yield RawDocument(
                url=str(resp.url),
                content=resp.content,
                http_status=resp.status_code,
                http_headers=dict(resp.headers),
                meta={"kind": "docket"},
            )
        for raw in pages:
            raw.meta["docket_info"] = docket_info
            yield raw

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        if raw.meta.get("kind") == "docket":
            return  # snapshotted for provenance; consumed via pages' docket_info
        try:
            body = json.loads(raw.content)
        except json.JSONDecodeError:
            return
        docket_info: dict[str, dict] = raw.meta.get("docket_info", {})
        for doc in body.get("data") or []:
            item = self._normalize_doc(doc, docket_info)
            if item is not None:
                yield item

    def _normalize_doc(self, doc: dict, docket_info: dict[str, dict]) -> NormalizedItem | None:
        attrs = doc.get("attributes") or {}
        native = TYPE_TO_NATIVE.get(attrs.get("documentType") or "")
        doc_id = doc.get("id")
        if native is None or not doc_id:
            return None
        docket_id = attrs.get("docketId")
        docket = docket_info.get(docket_id or "", {})
        if docket and docket.get("docketType") not in (None, "Rulemaking"):
            return None  # Nonrulemaking dockets are out of scope (spec 00)

        join_keys: list[tuple[str, str]] = []
        if attrs.get("frDocNum"):
            join_keys.append(("fr_doc_number", attrs["frDocNum"]))
        if docket_id:
            join_keys.append(("docket_id", docket_id))
        rin = (docket.get("rin") or "").strip()
        if rin and rin.lower() not in _RIN_SENTINELS:
            join_keys.append(("rin", rin))

        dates: list[NormalizedDate] = []
        cc = _eastern_date(attrs.get("commentEndDate"))
        if cc:
            dates.append(NormalizedDate(DateType.comment_close, cc, Confidence.published_firm))

        return NormalizedItem(
            source_system=self.name,
            external_key=("regsgov_doc", doc_id),
            # enrichment-by-design: every document on a watched docket attaches to the
            # same rulemaking item, so the reducer's external-key conflict guard must
            # not split them apart
            supplementary=True,
            jurisdiction=self.jurisdiction,
            title=attrs.get("title") or doc_id,
            url=f"https://www.regulations.gov/document/{doc_id}",
            native_status=native,
            track=NATIVE_TO_TRACK[native],
            join_keys=join_keys,
            dates=dates,
            native_meta={
                "docket_type": docket.get("docketType") or "",
                "open_for_comment": str(attrs.get("openForComment")),
                "agency": doc_id.split("-")[0] if "-" in doc_id else "",
            },
        )


def _eastern_date(value: str | None) -> date | None:
    """regulations.gov datetimes are UTC instants for 11:59:59 PM *Eastern*: the civil
    deadline date is the Eastern date, not the UTC one (observed: CIRCIA close
    2024-07-04T03:59:59Z == 2024-07-03 ET, matching FR's comments_close_on)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(_EASTERN).date() if dt.tzinfo else dt.replace(tzinfo=UTC).date()
