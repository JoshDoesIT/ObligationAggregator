from __future__ import annotations

import re
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
from oblag.db.models import Confidence, DateType

PAGE_URL = "https://cppa.ca.gov/regulations/"

# The page is organized by h2 sections; only formal rulemaking sections are in scope
# (spec 00: "Preliminary Rulemaking Activities" = pre-rule weak signals, excluded).
_SECTION_STATUS = {
    "Proposed Regulations": "proposed",
    "Completed Regulation Packages": "completed",
}
_H2_SPLIT = re.compile(r"<h2[^>]*>\s*([^<]{3,80}?)\s*</h2>", re.S)
_LINK_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_MONTH_YEAR_RE = re.compile(
    r"\((January|February|March|April|May|June|July|August|September|October|November"
    r"|December)\s+(20\d{2})\)"
)
_MONTHS = {
    m: n
    for n, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}


@register
class CppaAdapter(SourceAdapter):
    """California CPPA rulemaking page (static HTML): proposed regulation packages
    and completed packages under the state APA process."""

    name = "cppa"
    jurisdiction = "US-CA"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        resp = ctx.client.get(PAGE_URL)
        resp.raise_for_status()
        yield RawDocument(
            url=PAGE_URL,
            content=resp.content,
            content_type="text/html",
            http_status=resp.status_code,
            http_headers=dict(resp.headers),
        )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        html = raw.content.decode("utf-8", errors="replace")
        parts = _H2_SPLIT.split(html)
        # parts = [pre, h2a, body_a, h2b, body_b, …]
        sections = dict(zip(parts[1::2], parts[2::2], strict=False))
        matched_any = False
        seen: set[str] = set()
        for heading, body in sections.items():
            status = _SECTION_STATUS.get(heading.strip())
            if status is None:
                continue
            matched_any = True
            for href, inner in _LINK_RE.findall(body):
                title = re.sub(r"<[^>]+>|\s+", " ", inner).strip()
                title = title.replace("&ndash;", "–").replace("&amp;", "&")
                if not title or href.startswith(("#", "mailto:")):
                    continue
                key = href if href.startswith("http") else f"https://cppa.ca.gov{href}"
                if key in seen:
                    continue  # pages are linked repeatedly; first mention wins
                seen.add(key)
                dates: list[NormalizedDate] = []
                my = _MONTH_YEAR_RE.search(title)
                if my and status == "completed":
                    dates.append(
                        NormalizedDate(
                            DateType.adopted,
                            date(int(my.group(2)), _MONTHS[my.group(1)], 1),
                            Confidence.derived,  # month precision only
                        )
                    )
                yield NormalizedItem(
                    source_system=self.name,
                    external_key=("cppa_page", key),
                    jurisdiction=self.jurisdiction,
                    title=f"CPPA rulemaking: {title}",
                    url=key,
                    native_status=status,
                    track="proposed" if status == "proposed" else "final",
                    dates=dates,
                    obligation_slug="ccpa",
                )
        if not matched_any:
            # page restructured — surface as a parse anomaly item-free via a sentinel
            yield NormalizedItem(
                source_system=self.name,
                external_key=("cppa_page", PAGE_URL),
                jurisdiction=self.jurisdiction,
                title="CPPA rulemaking page",
                url=PAGE_URL,
                native_status="unknown",
                track="default",
                obligation_slug="ccpa",
                anomalies=["expected rulemaking sections not found; page restructured?"],
            )
