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

LISTING_URL = "https://www.eba.europa.eu/publications-and-media/consultations"
BASE = "https://www.eba.europa.eu"

_ROW_SPLIT = re.compile(r'class="[^"]*views-row')
_LINK_RE = re.compile(r'<a[^>]+href="(/publications-and-media/[^"]+)"')
_CP_REF_RE = re.compile(r"\(?(EBA/(?:CP|DP)/\d{4}/[\d/]+)\)?")
_MONTHS = {
    m: n
    for n, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}
# tag-stripped row text: "26 | Jun | 28 | Sep | 2026 | <title> | (EBA/CP/2026/…)"
_WINDOW_RE = re.compile(
    r"(\d{1,2})\s*\|\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"(?:\s*\|\s*(\d{4}))?"
    r"\s*\|\s*(\d{1,2})\s*\|\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s*\|\s*(\d{4})"
)
_DORA_RE = re.compile(r"(?i)\bDORA\b|digital operational resilience|ICT risk")


@register
class EbaAdapter(SourceAdapter):
    """EBA consultations via headless-browser rendering (spec 06 addendum: the site is
    Drupal with disabled JSON:API and a JS-rendered listing — probed live)."""

    name = "eba"
    jurisdiction = "EU"

    def enabled(self) -> bool:
        from oblag.browserfetch import browser_available

        return browser_available()

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        from oblag.browserfetch import render_page

        yield render_page(LISTING_URL, wait_selector=".views-row", timeout_s=60)

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        html = raw.content.decode("utf-8", errors="replace")
        rows = _ROW_SPLIT.split(html)[1:]
        for row in rows:
            item = self._normalize_row(row)
            if item is not None:
                yield item
        if not rows:
            yield NormalizedItem(
                source_system=self.name,
                external_key=("eba_page", LISTING_URL),
                jurisdiction=self.jurisdiction,
                title="EBA consultations listing",
                url=LISTING_URL,
                native_status="unknown",
                track="default",
                anomalies=["no consultation rows in rendered listing; page changed?"],
            )

    def _normalize_row(self, row: str) -> NormalizedItem | None:
        link_match = _LINK_RE.search(row)
        text = re.sub(r"<[^>]+>", "§", row)
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"(?:\s*§\s*)+", " | ", text)  # collapse runs of empty tags

        window = _WINDOW_RE.search(text)
        # title = the longest human text chunk after the date block
        chunks = [c.strip() for c in text.split(" | ") if len(c.strip()) >= 15]
        title = chunks[0] if chunks else None
        if not title or not link_match:
            return None
        cp_ref = _CP_REF_RE.search(text)

        dates: list[NormalizedDate] = []
        anomalies: list[str] = []
        if window:
            close_year = int(window.group(6))
            if window.group(3):
                open_year = int(window.group(3))
            elif _MONTHS[window.group(2)] > _MONTHS[window.group(5)]:
                # rows omit the open year; a window spanning New Year
                # ("5 Dec … 5 Mar 2025") opened the year before it closes
                open_year = close_year - 1
            else:
                open_year = close_year
            try:
                dates.append(
                    NormalizedDate(
                        DateType.comment_open,
                        date(open_year, _MONTHS[window.group(2)], int(window.group(1))),
                        Confidence.published_firm,
                    )
                )
                dates.append(
                    NormalizedDate(
                        DateType.comment_close,
                        date(int(window.group(6)), _MONTHS[window.group(5)], int(window.group(4))),
                        Confidence.published_firm,
                    )
                )
            except ValueError:
                anomalies.append(f"unparseable consultation window in row for {title!r}")
        else:
            anomalies.append(f"no consultation window found for {title!r}")

        external = (
            ("eba_cp", cp_ref.group(1)) if cp_ref else ("eba_page", BASE + link_match.group(1))
        )
        return NormalizedItem(
            source_system=self.name,
            external_key=external,
            jurisdiction=self.jurisdiction,
            title=title,
            url=BASE + link_match.group(1),
            native_status="consultation",
            track="proposed",
            dates=dates,
            join_keys=[("eba_page", BASE + link_match.group(1))] if cp_ref else [],
            obligation_slug="dora" if _DORA_RE.search(title) else None,
            anomalies=anomalies,
        )
