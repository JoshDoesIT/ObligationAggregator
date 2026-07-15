from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, timedelta
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

FEED_URL = "https://blog.pcisecuritystandards.org/rss.xml"

# Formal signals only (spec 06): RFC announcements. Blog noise never becomes an item.
_RFC_RE = re.compile(r"^\s*Request for Comments:?\s*(?P<subject>.+?)\s*$", re.IGNORECASE)
RFC_MIN_DAYS = 30  # PCI SSC minimum RFC duration — basis for the derived close date


@register
class PciSscAdapter(SourceAdapter):
    name = "pci_ssc"
    jurisdiction = "Global"

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
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            match = _RFC_RE.match(title)
            if not match:
                continue
            link = (item.findtext("link") or "").strip()
            pub = _parse_rfc822_date(item.findtext("pubDate"))
            subject = match.group("subject")

            dates: list[NormalizedDate] = []
            anomalies: list[str] = []
            if pub:
                dates.append(NormalizedDate(DateType.comment_open, pub, Confidence.published_firm))
                # RFC close dates live behind the PO portal (NDA); the minimum duration
                # gives a floor — explicitly `derived`, never presented as firm.
                dates.append(
                    NormalizedDate(
                        DateType.comment_close,
                        pub + timedelta(days=RFC_MIN_DAYS),
                        Confidence.derived,
                    )
                )
            else:
                anomalies.append(f"RFC item without parseable pubDate: {title!r}")

            yield NormalizedItem(
                source_system=self.name,
                external_key=("pci_doc", _slug(subject)),
                jurisdiction=self.jurisdiction,
                title=f"PCI SSC RFC: {subject}",
                url=link or None,
                native_status="rfc",
                track="proposed",
                dates=dates,
                obligation_slug="pci-dss" if "pci dss" in subject.lower() else None,
                native_meta={"blog_title": title},
                anomalies=anomalies,
            )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _parse_rfc822_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip()).date()
    except (TypeError, ValueError):
        return None
