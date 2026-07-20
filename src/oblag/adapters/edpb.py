from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from email.utils import parsedate_to_datetime

from defusedxml import ElementTree

from oblag.adapters import register
from oblag.adapters.base import (
    FetchContext,
    NormalizedDate,
    NormalizedItem,
    RawDocument,
    SourceAdapter,
)
from oblag.db.models import Confidence, DateType

FEED_URL = "https://www.edpb.europa.eu/feed/news_en"

# Formal signals only (spec 00): consultation launches and adopted guidance.
_CONSULTATION_RE = re.compile(r"(?i)\b(public\s+)?consultation\b")
_ADOPTED_RE = re.compile(r"(?i)\badopt(s|ed)\b.*\b(guidelines|recommendations|opinion)\b")
# "... open until 15 September 2026" style deadlines in title/description
_DEADLINE_RE = re.compile(
    r"(?i)(?:until|by|deadline[^0-9]{0,20})\s*(\d{1,2}\s+[A-Z][a-z]+\s+20\d{2})"
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
class EdpbAdapter(SourceAdapter):
    """EDPB news RSS filtered to formal signals: consultation launches (comment
    windows on GDPR guidance) and adopted guidelines/recommendations."""

    name = "edpb"
    jurisdiction = "EU"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        resp = ctx.client.get(FEED_URL)
        resp.raise_for_status()
        yield RawDocument(
            url=FEED_URL,
            content=resp.content,
            content_type="application/rss+xml",
            http_status=resp.status_code,
            http_headers=dict(resp.headers),
        )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        try:
            root = ElementTree.fromstring(raw.content)
        except ElementTree.ParseError:
            return
        for entry in root.iter("item"):
            title = (entry.findtext("title") or "").strip()
            link = (entry.findtext("link") or "").strip()
            description = entry.findtext("description") or ""
            if not title or not link:
                continue
            is_consultation = bool(_CONSULTATION_RE.search(title))
            is_adopted = bool(_ADOPTED_RE.search(title))
            if not (is_consultation or is_adopted):
                continue  # ordinary news: weak signal
            pub = _parse_rfc822(entry.findtext("pubDate"))

            dates: list[NormalizedDate] = []
            anomalies: list[str] = []
            if is_consultation:
                if pub:
                    dates.append(
                        NormalizedDate(DateType.comment_open, pub, Confidence.published_firm)
                    )
                deadline = _parse_deadline(f"{title} {description}")
                if deadline:
                    dates.append(
                        NormalizedDate(DateType.comment_close, deadline, Confidence.published_firm)
                    )
                else:
                    anomalies.append(f"consultation without parseable deadline: {title!r}")
                native, track = "consultation", "proposed"
            else:
                if pub:
                    dates.append(NormalizedDate(DateType.adopted, pub, Confidence.published_firm))
                native, track = "adopted", "final"

            yield NormalizedItem(
                source_system=self.name,
                external_key=("edpb_item", link),
                jurisdiction=self.jurisdiction,
                title=title,
                url=link,
                native_status=native,
                track=track,
                dates=dates,
                obligation_slug="gdpr",
                anomalies=anomalies,
            )


def _parse_rfc822(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip()).date()
    except (TypeError, ValueError):
        return None


def _parse_deadline(text: str) -> date | None:
    m = _DEADLINE_RE.search(text)
    if not m:
        return None
    try:
        day, month_name, year = m.group(1).split()
        return date(int(year), _MONTHS[month_name], int(day))
    except (KeyError, ValueError):
        return None
