from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime

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

FEED_URL = "https://www.esma.europa.eu/rss.xml"

_CONSULT_RE = re.compile(r"(?i)\bconsult(s|ation)\b")
# item dates live inside the Drupal-rendered description markup
_DATETIME_RE = re.compile(r'datetime="(\d{4}-\d{2}-\d{2})T')
_DORA_RE = re.compile(r"(?i)\bDORA\b|digital operational resilience")


@register
class EsmaAdapter(SourceAdapter):
    """ESMA all-news RSS filtered to consultation launches (DORA RTS/ITS and other
    ESMA mandates). Titles like 'ESMA consults on …' are the formal signal."""

    name = "esma"
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
            if not title or not link or not _CONSULT_RE.search(title):
                continue
            description = entry.findtext("description") or ""
            published = _first_date(description)

            dates: list[NormalizedDate] = []
            anomalies: list[str] = []
            if published:
                dates.append(
                    NormalizedDate(DateType.comment_open, published, Confidence.published_firm)
                )
            else:
                anomalies.append(f"consultation item without parseable date: {title!r}")

            yield NormalizedItem(
                source_system=self.name,
                external_key=("esma_item", link),
                jurisdiction=self.jurisdiction,
                title=title,
                url=link,
                native_status="consultation",
                track="proposed",
                dates=dates,
                obligation_slug="dora" if _DORA_RE.search(title) else None,
                anomalies=anomalies,
            )


def _first_date(text: str) -> date | None:
    m = _DATETIME_RE.search(text)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1)).date()
    except ValueError:
        return None
