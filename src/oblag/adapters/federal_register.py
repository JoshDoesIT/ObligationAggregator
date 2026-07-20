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

BASE = "https://www.federalregister.gov/api/v1"

FIELDS = [
    "document_number",
    "title",
    "type",
    "abstract",
    "action",
    "publication_date",
    "comments_close_on",
    "effective_on",
    "regulation_id_numbers",
    "docket_ids",
    "html_url",
    "agencies",
    "significant",
]

# API returns human-readable type strings; queries use the short codes.
TYPE_TO_NATIVE = {"Proposed Rule": "PRORULE", "Rule": "RULE"}
NATIVE_TO_TRACK = {"PRORULE": "proposed", "RULE": "final"}

# Agency-wide umbrella RINs shared by every action of a routine category — NOT
# identifying, so never emitted as join keys (they falsely merged/linked distinct
# rulemakings, observed live). FAA: airworthiness directives, standard instrument
# approach procedures, airspace amendments. USCG: safety zones, special local
# regulations, drawbridge operations, anchorages, security zones.
UMBRELLA_RINS = frozenset(
    {
        "2120-AA64",
        "2120-AA65",
        "2120-AA66",
        "1625-AA00",
        "1625-AA08",
        "1625-AA09",
        "1625-AA11",
        "1625-AA87",
    }
)


@register
class FederalRegisterAdapter(SourceAdapter):
    name = "federal_register"
    jurisdiction = "US-Federal"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        for doc_type in ("PRORULE", "RULE"):
            params: list[tuple[str, str | int | float | bool | None]] = [
                ("conditions[type][]", doc_type),
                ("per_page", "250"),
                ("order", "oldest"),
            ]
            params.extend(("fields[]", f) for f in FIELDS)
            if ctx.window:
                params.append(("conditions[publication_date][gte]", ctx.window[0].isoformat()))
                params.append(("conditions[publication_date][lte]", ctx.window[1].isoformat()))
            elif ctx.since:
                params.append(("conditions[publication_date][gte]", ctx.since.date().isoformat()))
            for agency in ctx.params.get("agencies", []):
                params.append(("conditions[agencies][]", agency))

            url: str | None = f"{BASE}/documents.json"
            first = True
            while url:
                resp = ctx.client.get(url, params=params if first else None)
                resp.raise_for_status()
                yield RawDocument(
                    url=str(resp.url),
                    content=resp.content,
                    http_status=resp.status_code,
                    http_headers=dict(resp.headers),
                )
                url = resp.json().get("next_page_url")
                first = False

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        try:
            page = json.loads(raw.content)
        except json.JSONDecodeError:
            return
        for doc in page.get("results") or []:
            item = self._normalize_doc(doc)
            if item is not None:
                yield item

    def _normalize_doc(self, doc: dict) -> NormalizedItem | None:
        native = TYPE_TO_NATIVE.get(doc.get("type", ""))
        doc_number = doc.get("document_number")
        if native is None or not doc_number:
            return None
        action = (doc.get("action") or "").strip()
        action_lower = action.lower()
        # Scope boundary (spec 00): ANPRMs are weak signals, excluded by default.
        if "advance notice" in action_lower and not get_settings().include_prerule:
            return None

        anomalies: list[str] = []
        dates: list[NormalizedDate] = []
        # Supplementary documents (extension/correction/delay/reopening/withdrawal) share the
        # rulemaking's join keys but are not the root document: their publication_date is not
        # the rulemaking's proposal/adoption date.
        is_supplementary = any(
            word in action_lower
            for word in ("extension", "correction", "reopen", "withdraw", "delay")
        )
        pub = _parse_date(doc.get("publication_date"))
        if pub and not is_supplementary:
            pub_type = DateType.proposal_date if native == "PRORULE" else DateType.adopted
            dates.append(NormalizedDate(pub_type, pub, Confidence.published_firm))

        is_correction = "correction" in action_lower
        for field_name, date_type in (
            ("comments_close_on", DateType.comment_close),
            ("effective_on", DateType.effective),
        ):
            value = _parse_date(doc.get(field_name))
            if value is None:
                continue
            if is_correction:
                # Corrections routinely carry stale/bogus date metadata (observed live:
                # CIRCIA correction 2024-12084 lists comments_close_on before the real
                # extended close). Corrections never move deadlines; extensions do.
                anomalies.append(
                    f"correction document {doc_number} carried {field_name}={value}; ignored"
                )
                continue
            dates.append(NormalizedDate(date_type, value, Confidence.published_firm))

        join_keys: list[tuple[str, str]] = []
        for rin in doc.get("regulation_id_numbers") or []:
            if rin not in UMBRELLA_RINS:
                join_keys.append(("rin", rin))
        for docket in doc.get("docket_ids") or []:
            join_keys.append(("docket_id", _clean_docket(docket)))

        agencies = doc.get("agencies") or []
        agency_slugs = ",".join(
            sorted(a.get("slug") or a.get("name", "") for a in agencies if isinstance(a, dict))
        )
        return NormalizedItem(
            source_system=self.name,
            external_key=("fr_doc_number", doc_number),
            jurisdiction=self.jurisdiction,
            title=doc.get("title") or doc_number,
            abstract=doc.get("abstract"),
            url=doc.get("html_url"),
            native_status=native,
            track=NATIVE_TO_TRACK[native],
            join_keys=join_keys,
            dates=dates,
            native_meta={
                "action": action,
                "agencies": agency_slugs,
                "significant": str(doc.get("significant")),
            },
            anomalies=anomalies,
            supplementary=is_supplementary,
        )


def _clean_docket(value: str) -> str:
    """FR returns 'Docket No. CISA-2022-0010'; regulations.gov uses bare 'CISA-2022-0010'.
    Canonicalize so the docket_id join key correlates across sources."""
    v = value.strip()
    for prefix in ("Docket No. ", "Docket Number ", "Docket ID ", "Docket "):
        if v.lower().startswith(prefix.lower()):
            return v[len(prefix) :].strip()
    return v


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
