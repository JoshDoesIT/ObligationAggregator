from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from oblag.adapters import register
from oblag.adapters.base import FetchContext, NormalizedItem, RawDocument, SourceAdapter
from oblag.config import get_settings

BASE = "https://oeil.europarl.europa.eu/oeil/en/procedure-file"

_TITLE_RE = re.compile(r"<title>Procedure File:\s*([^|<]+)", re.IGNORECASE)
_SUBJECT_RE = re.compile(
    r'class="[^"]*erpl-title-h1[^"]*"[^>]*>\s*([^<]{5,300}?)\s*<', re.IGNORECASE
)
_STAGE_RE = re.compile(r"Stage reached[^<]*</[^>]+>\s*<[^>]+>\s*([^<]{3,120}?)\s*<")
_EVENT_ROW_RE = re.compile(r"(\d{2}/\d{2}/\d{4})")


@register
class OeilAdapter(SourceAdapter):
    """OEIL watched procedures: conservative scraper for 'Stage reached' + key events.

    No bulk API exists (spec 05 spike); only procedures the user explicitly watches
    (OBLAG_OEIL_PROCEDURES or ctx.params['procedures']) are fetched."""

    name = "oeil"
    jurisdiction = "EU"

    def _watched(self, ctx: FetchContext) -> list[str]:
        procs = ctx.params.get("procedures")
        if procs:
            return list(procs)
        raw = get_settings().oeil_procedures or ""
        return [p.strip() for p in raw.split(",") if p.strip()]

    def enabled(self) -> bool:
        return bool(get_settings().oeil_procedures)

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        for reference in self._watched(ctx):
            resp = ctx.client.get(BASE, params={"reference": reference})
            resp.raise_for_status()
            yield RawDocument(
                url=str(resp.url),
                content=resp.content,
                content_type="text/html",
                http_status=resp.status_code,
                http_headers=dict(resp.headers),
                meta={"reference": reference},
            )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        reference = raw.meta.get("reference")
        if not reference:
            return
        html = raw.content.decode("utf-8", errors="replace")
        anomalies: list[str] = []

        stage_match = _STAGE_RE.search(html)
        stage = stage_match.group(1).strip() if stage_match else ""
        if not stage:
            anomalies.append(f"could not parse 'Stage reached' for {reference}")
            stage = "unknown"

        title_match = _SUBJECT_RE.search(html) or _TITLE_RE.search(html)
        title = title_match.group(1).strip() if title_match else f"Procedure {reference}"

        # key-event dates fingerprint: new events surface as content_changed
        event_dates = _EVENT_ROW_RE.findall(html)
        events_digest = hashlib.sha256(("|".join(sorted(set(event_dates)))).encode()).hexdigest()[
            :16
        ]

        yield NormalizedItem(
            source_system=self.name,
            external_key=("oeil_procedure", reference),
            jurisdiction=self.jurisdiction,
            title=title,
            url=raw.url,
            native_status=stage,
            track="proposed",
            native_meta={"events_digest": events_digest, "event_count": str(len(event_dates))},
            anomalies=anomalies,
        )
