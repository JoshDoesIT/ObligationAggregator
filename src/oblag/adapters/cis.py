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

BLOG_FEED = "https://www.cisecurity.org/feed/blog"

# Strict release signal only: "CIS Controls v8.1 …". Community/marketing posts and the
# vulnerability-advisory feed are noise for framework tracking.
_RELEASE_RE = re.compile(r"(?i)\bCIS\s+(?:Critical\s+Security\s+)?Controls\s+v(\d+(?:\.\d+)?)")


@register
class CisAdapter(SourceAdapter):
    """CIS blog RSS filtered to Controls version releases (low yield by design)."""

    name = "cis"
    jurisdiction = "Global"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        resp = ctx.client.get(BLOG_FEED)
        resp.raise_for_status()
        yield RawDocument(
            url=BLOG_FEED,
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
            match = _RELEASE_RE.search(title)
            if not match:
                continue
            link = (entry.findtext("link") or "").strip()
            version = match.group(1)
            pub = _parse_rfc822(entry.findtext("pubDate"))
            dates: list[NormalizedDate] = []
            if pub:
                dates.append(NormalizedDate(DateType.effective, pub, Confidence.derived))
            yield NormalizedItem(
                source_system=self.name,
                external_key=("cis_release", version),
                jurisdiction=self.jurisdiction,
                title=f"CIS Controls v{version}",
                url=link or None,
                native_status="release",
                track="final",
                dates=dates,
                obligation_slug="cis-controls",
                native_meta={"blog_title": title},
            )


def _parse_rfc822(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip()).date()
    except (TypeError, ValueError):
        return None
