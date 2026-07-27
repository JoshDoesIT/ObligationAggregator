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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

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
        url="https://www.dfs.ny.gov/industry_guidance/cybersecurity",
        # DFS states its amendments as a sentence with the date in it
        pattern=re.compile(
            r"On (\w+ \d{1,2}, \d{4}), DFS announced amendments to Cybersecurity Regulation",
            re.IGNORECASE,
        ),
        title="23 NYCRR Part 500: amendments announced {}",
        captures_date=True,
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
                content_type="text/html",
                http_status=resp.status_code,
                http_headers=dict(resp.headers),
                meta={"page": page.key},
            )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        page = next((p for p in WATCHED if p.key == raw.meta.get("page")), None)
        if page is None:
            return
        text = _plain(raw.content.decode("utf-8", errors="replace"))
        if _SUPERSEDED.search(text):
            # the page says it is not the current one, so believe it and record nothing
            # rather than publishing a version the body has already replaced
            return
        match = _best_match(page, text)
        if match is None:
            return
        value = match.group(1)
        if not value:
            return
        if page.captures_date:
            stated = _parse_date(value)
        elif page.released_group:
            stated = _parse_date(match.group(page.released_group))
        else:
            stated = None
        yield NormalizedItem(
            source_system=self.name,
            # one row per watched page: when the page states a new version this row
            # updates, which is exactly the change we are here to catch
            external_key=("watched_page", page.key),
            jurisdiction=page.jurisdiction,
            title=page.title.format(value),
            url=page.url,
            native_status="current",
            track="final",
            obligation_slug=page.obligation,
            published_at=stated,
            dates=(
                [NormalizedDate(DateType.effective, stated, Confidence.published_firm)]
                if stated
                else []
            ),
            native_meta=({"stated": value} if page.captures_date else {"published_version": value}),
        )


def _best_match(page: WatchedPage, text: str) -> re.Match[str] | None:
    """The HIGHEST version the page states, not the first one it happens to mention.

    Observed live: two fetches of the same CIS URL minutes apart came back as different
    CDN variants, and in one of them "CIS Controls v7.1" appeared before v8.1 — which
    would have published a superseded edition as the current one. A page about v8.1
    mentions older versions all the time (upgrade notes, mapping tables); it never
    mentions a version newer than the one it is about.

    Dates are left as first-match: a page states one amendment announcement, and
    "highest" is not a meaningful ordering for the sentence forms we parse.
    """
    matches = list(page.pattern.finditer(text))
    if not matches:
        return None
    if page.captures_date:
        return matches[0]
    return max(matches, key=lambda m: _version_key(m.group(1)))


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
