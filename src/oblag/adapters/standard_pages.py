"""The one page a body keeps its current version on.

Some obligations have no feed, no API and no document library — just a page that says
what the current version is and when it landed. CIS Controls, the CSA Cloud Controls
Matrix and NYDFS 23 NYCRR 500 are all like that, and all three showed nothing at all
because every adapter we had was looking for a stream of events.

So this one watches pages instead of feeds. Each entry names the page, a pattern that
extracts the version or amendment the page states, and the obligation it belongs to.
When the page starts saying something different, that IS the change signal — which is
the same idea as iso_catalog and nist_pubs, generalised to bodies that publish nothing
else.

Deliberately narrow. A pattern must anchor on words the body uses ABOUT its own
standard, never a bare version number: the CSA page carries `?ver=4.0.13` on a
WordPress asset, and a loose `v(\\d+\\.\\d+)` matched that instead of the standard.
Anything that does not match yields nothing rather than a guess.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from oblag.adapters import register
from oblag.adapters.base import (
    FetchContext,
    NormalizedDate,
    NormalizedItem,
    RawDocument,
    SourceAdapter,
)
from oblag.db.models import Confidence, DateType


@dataclass(frozen=True)
class WatchedPage:
    key: str  # identity of the row this page maintains
    obligation: str
    jurisdiction: str
    url: str
    # must capture the version (or the amendment date) the page states
    pattern: re.Pattern[str]
    title: str  # "{}" is filled with the captured value
    # when the capture is a date rather than a version string
    captures_date: bool = False
    # optional second group: the date the page says that version was released
    released_group: int | None = None
    # match the URL the fetch RESOLVED TO rather than the body. Some bodies state the
    # current text by what a stable link points at, not by prose — see nydfs-500.
    match_url: bool = False


# A body telling us "this page is not the current one" outranks anything else on it.
# CSA leaves its superseded artifact pages up and prints this at the top; without the
# guard we would have recorded CCM v4.0 (2021) as current while v4.1 (2026) was out.
_SUPERSEDED = re.compile(r"There is a new version of", re.IGNORECASE)


WATCHED: tuple[WatchedPage, ...] = (
    WatchedPage(
        key="cis-controls",
        obligation="cis-controls",
        jurisdiction="Global",
        url="https://www.cisecurity.org/controls/v8-1",
        # CIS names the release in prose all over this page; the digits alone appear in
        # asset URLs, so the words have to be part of the match
        pattern=re.compile(r"CIS Controls\s+v(\d+(?:\.\d+)*)", re.IGNORECASE),
        title="CIS Critical Security Controls v{}",
    ),
    WatchedPage(
        key="csa-ccm",
        obligation="csa-ccm",
        jurisdiction="Global",
        url="https://cloudsecurityalliance.org/artifacts/cloud-controls-matrix-v4-1",
        # CSA prints its own release line. Anchoring on it also keeps us off the
        # WordPress asset version (?ver=4.0.13), which a bare v(\d+\.\d+) matched.
        pattern=re.compile(
            r"Cloud Controls Matrix[^.]{0,40}?v(\d+(?:\.\d+)*)\s*Released:\s*(\d{2}/\d{2}/\d{4})",
            re.IGNORECASE,
        ),
        title="CSA Cloud Controls Matrix v{}",
        released_group=2,
    ),
    WatchedPage(
        key="nist-ai-rmf",
        obligation="nist-ai-rmf",
        jurisdiction="US-Federal",
        url="https://www.nist.gov/itl/ai-risk-management-framework",
        # AI 100-1 has no CSRC series index (/publications/ai is a 404), so nist_pubs
        # cannot see it and this page is the only surface that states the version. NIST
        # writes "AI RMF 1.0" throughout, including in the line announcing that 1.0 is
        # being revised, which is exactly the signal we want to catch when 2.0 lands.
        pattern=re.compile(r"AI RMF\s+(\d+(?:\.\d+)*)", re.IGNORECASE),
        title="NIST AI Risk Management Framework {}",
    ),
    WatchedPage(
        key="nydfs-500",
        obligation="nydfs-500",
        jurisdiction="US-NY",
        # A stable alias that 302s to the regulation text. DFS rebuilt this site and the
        # sentence we used to parse ("On November 1, 2023, DFS announced amendments to
        # Cybersecurity Regulation") is gone from every HTML page it has: the old URL now
        # redirects to a link hub, and the word "amendment" appears nowhere on the hub,
        # the requirements page or the FAQs. The regulation is served as one consolidated
        # PDF instead, so what the body states about currency is WHERE THIS LINK POINTS.
        url="https://www.dfs.ny.gov/cybersecurity/23-NYCRR-Part-500",
        # DFS files documents under a dated CMS path, so republishing the text moves the
        # link. Anchored on the filename as well as the date so an unrelated document
        # sharing the /documents/YYYY/MM/ prefix cannot satisfy it.
        pattern=re.compile(r"/documents/(\d{4}/\d{2})/[^/]*part-500[^/]*\.pdf", re.IGNORECASE),
        title="23 NYCRR Part 500: regulation text posted {}",
        match_url=True,
    ),
)

_TAGS = re.compile(r"<(script|style|svg).*?</\1>", re.S | re.I)


@register
class StandardPagesAdapter(SourceAdapter):
    """Current version of standards whose only publication surface is one page."""

    name = "standard_pages"
    jurisdiction = "Global"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        for page in WATCHED:
            resp = ctx.client.get(page.url)
            resp.raise_for_status()
            yield RawDocument(
                url=page.url,
                content=resp.content,
                content_type=resp.headers.get("content-type", "text/html"),
                http_status=resp.status_code,
                http_headers=dict(resp.headers),
                # resolved_url is what the fetch ended on after redirects, which for
                # match_url pages IS the signal. expect_item makes a page that stops
                # matching show up on adapter health instead of vanishing quietly.
                meta={"page": page.key, "resolved_url": str(resp.url), "expect_item": "1"},
            )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        page = next((p for p in WATCHED if p.key == raw.meta.get("page")), None)
        if page is None:
            return
        if page.match_url:
            # the body is the document itself (a PDF here), so there is nothing to read
            # for prose and nothing to check for a supersession notice
            haystack = raw.meta.get("resolved_url", "")
        else:
            haystack = _plain(raw.content.decode("utf-8", errors="replace"))
            if _SUPERSEDED.search(haystack):
                # the page says it is not the current one, so believe it and record
                # nothing rather than publishing a version the body has already replaced
                return
        match = _best_match(page, haystack)
        if match is None:
            return
        value = match.group(1)
        if not value:
            return
        if page.captures_date:
            stated = _parse_date(value)
        elif page.released_group:
            stated = _parse_date(match.group(page.released_group))
        elif page.match_url:
            # the document's own Last-Modified is an exact date; the dated path it sits
            # under is only ever a month, so prefer the header and fall back to neither
            stated = _http_date(raw.http_headers.get("last-modified"))
        else:
            stated = None
        yield NormalizedItem(
            source_system=self.name,
            # one row per watched page: when the page states a new version this row
            # updates, which is exactly the change we are here to catch
            external_key=("watched_page", page.key),
            jurisdiction=page.jurisdiction,
            title=page.title.format(_shown(page, value, stated)),
            url=page.url,
            native_status="current",
            track="final",
            obligation_slug=page.obligation,
            published_at=stated,
            # this row reports what one page says today, so when the body reissues its
            # standard the date has to follow the title rather than stay at whatever we
            # first saw. NYDFS shipped with the title reading 16 July 2026 and
            # published_at still on 2023-11-01, which is worse than either alone.
            published_at_moves=True,
            dates=(
                # a match_url date is the moment the body last wrote the file to its CMS,
                # which says the text was reissued and nothing at all about when it takes
                # effect. published_at carries it; asserting effective from it would be a
                # claim the source never made.
                [NormalizedDate(DateType.effective, stated, Confidence.published_firm)]
                if stated and not page.match_url
                else []
            ),
            native_meta=({"stated": value} if page.captures_date else {"published_version": value}),
        )


def _best_match(page: WatchedPage, text: str) -> re.Match[str] | None:
    """The version the page states MOST OFTEN, ties broken by the highest.

    Two failure modes, both observed live on the same CIS page:

    * First-match is wrong. Two fetches minutes apart came back as different CDN
      variants, and one mentioned "CIS Controls v7.1" before v8.1 — first-match would
      have published a superseded edition as the current standard.
    * Highest-match is wrong too, which is what replacing it with `max` then caused.
      CIS lists companion documents on the same page, and a white paper titled
      "CIS Controls v8.1.2 AI Security Guidance Workbook" made the row claim a Controls
      release that does not exist.

    Counting fixes both, because a page about v8.1 says v8.1 over and over (seven times
    here) while an incidental mention appears once. The highest tie-break keeps the
    original CDN-variant fix working when every mention is equally rare.

    Dates are left as first-match: a page states one amendment announcement, and
    "most often" is not a meaningful ordering for the sentence forms we parse.
    """
    matches = list(page.pattern.finditer(text))
    if not matches:
        return None
    if page.captures_date:
        return matches[0]
    counts = Counter(m.group(1) for m in matches)
    winner = max(counts, key=lambda v: (counts[v], _version_key(v)))
    return next(m for m in matches if m.group(1) == winner)


def _shown(page: WatchedPage, value: str, stated: date | None) -> str:
    """What goes in the title. A dated CMS path is only ever a month, so when the
    document's own timestamp gives an exact day, say the day."""
    if page.match_url and stated:
        return f"{stated.day} {stated.strftime('%B %Y')}"
    return value


def _http_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", value)) or (0,)


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _TAGS.sub("", html)))


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None
