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
RFC_MIN_DAYS = 30  # PCI SSC minimum RFC duration — fallback floor for the close date

# Announcement bodies state the real window: "From 3 June to 20 July, eligible
# PCI SSC stakeholders are invited…". Day-first, month by name, no year.
_WINDOW_RE = re.compile(
    r"From\s+(?P<open_day>\d{1,2})\s+(?P<open_month>[A-Za-z]+)"
    r"\s+to\s+(?P<close_day>\d{1,2})\s+(?P<close_month>[A-Za-z]+)"
)
_MONTHS = {
    name: i
    for i, name in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}


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
            window = _parse_window(item.findtext("description"), anchor=pub)
            if window:
                opened, closed = window
                dates.append(
                    NormalizedDate(DateType.comment_open, opened, Confidence.published_firm)
                )
                dates.append(
                    NormalizedDate(DateType.comment_close, closed, Confidence.published_firm)
                )
            elif pub:
                dates.append(NormalizedDate(DateType.comment_open, pub, Confidence.published_firm))
                # No stated window: the minimum RFC duration gives a floor —
                # explicitly `derived`, never presented as firm.
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
                obligation_slug=_pci_obligation(subject),
                native_meta={"blog_title": title},
                anomalies=anomalies,
            )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# RFC subject → catalog slug for the PCI SSC standards family (first match wins)
_PCI_SLUG_RULES = [
    (r"pci dss|data security standard", "pci-dss"),
    (r"pts hsm|hardware security module", "pci-pts-hsm"),
    (r"pts poi|point of interaction", "pci-pts-poi"),
    (r"pin security|\bpin\b(?!.*transaction)", "pci-pin"),
    (r"point.to.point encryption|p2pe", "pci-p2pe"),
    (r"3ds", "pci-3ds"),
    (r"secure software lifecycle|secure slc", "pci-secure-slc"),
    (r"secure software", "pci-secure-software"),
    (r"card production", "pci-card-production"),
    (r"token service", "pci-tsp"),
    (r"mobile payments? on cots|mpoc", "pci-mpoc"),
    (r"key management operations|\bkmo\b", "pci-kmo"),
]


def _pci_obligation(subject: str) -> str | None:
    lowered = subject.lower()
    for pattern, slug in _PCI_SLUG_RULES:
        if re.search(pattern, lowered):
            return slug
    return None


def _parse_window(description: str | None, anchor: date | None) -> tuple[date, date] | None:
    """Extract the stated RFC window from the announcement body.

    Dates carry no year: the open date takes the year that lands it closest to the
    announcement's pubDate, and a close month earlier than the open month rolls into
    the next year ("From 24 November to 9 January")."""
    if not description or anchor is None:
        return None
    m = _WINDOW_RE.search(description)
    if m is None:
        return None
    open_month = _MONTHS.get(m.group("open_month").lower())
    close_month = _MONTHS.get(m.group("close_month").lower())
    if open_month is None or close_month is None:
        return None
    try:
        opened = min(
            (date(anchor.year + off, open_month, int(m.group("open_day"))) for off in (-1, 0, 1)),
            key=lambda d: abs((d - anchor).days),
        )
        close_year = opened.year + 1 if close_month < open_month else opened.year
        closed = date(close_year, close_month, int(m.group("close_day")))
    except ValueError:  # e.g. "31 February" in a malformed post
        return None
    return opened, closed


def _parse_rfc822_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip()).date()
    except (TypeError, ValueError):
        return None
