from __future__ import annotations

import re
from collections.abc import Iterable

from oblag.adapters import register
from oblag.adapters.base import (
    FetchContext,
    NormalizedItem,
    RawDocument,
    SourceAdapter,
)

PAGE_URL = "https://www.nerc.com/pa/Stand/Pages/Standards-Under-Development.aspx"

# Conservative extraction: "Project 2025-03 <name>" mentions on the static page.
_PROJECT_RE = re.compile(r"Project\s+(\d{4}-\d{2})\s*([^\"<\[]{0,80})")


@register
class NercAdapter(SourceAdapter):
    """NERC standards-under-development page (static): reliability-standard projects
    as pipeline items; project appearance/changes drive events. Ballot/comment dates
    live in per-project SharePoint documents — tracked via curated assertions."""

    name = "nerc"
    jurisdiction = "US-Federal"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        resp = ctx.client.get(PAGE_URL, headers={"Accept": "text/html"})
        resp.raise_for_status()
        yield RawDocument(
            url=str(resp.url),
            content=resp.content,
            content_type="text/html",
            http_status=resp.status_code,
            http_headers=dict(resp.headers),
        )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        html = raw.content.decode("utf-8", errors="replace")
        seen: dict[str, str] = {}
        for number, tail in _PROJECT_RE.findall(html):
            name = re.sub(r"\s+", " ", tail).strip(" -–:.,")
            if number not in seen or len(name) > len(seen[number]):
                seen[number] = name
        if not seen:
            yield NormalizedItem(
                source_system=self.name,
                external_key=("nerc_page", PAGE_URL),
                jurisdiction=self.jurisdiction,
                title="NERC standards under development",
                url=PAGE_URL,
                native_status="unknown",
                track="default",
                obligation_slug="nerc-cip",
                anomalies=["no development projects parsed; page restructured?"],
            )
            return
        for number, name in sorted(seen.items()):
            title = f"NERC Project {number}"
            if name:
                title += f": {name}"
            yield NormalizedItem(
                source_system=self.name,
                external_key=("nerc_project", number),
                jurisdiction=self.jurisdiction,
                title=title,
                url=PAGE_URL,
                native_status="under_development",
                track="proposed",
                obligation_slug="nerc-cip",
            )
