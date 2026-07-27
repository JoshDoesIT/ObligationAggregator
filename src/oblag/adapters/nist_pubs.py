"""NIST publications of record, from the CSRC series indexes.

The existing nist_csrc adapter reads the "drafts open for comment" feed, which by
definition only carries what is in flight right now. That left every NIST obligation
in the catalog blank between revisions — SP 800-53, SP 800-171, SP 800-63, FIPS 140-3,
the CSF and the Privacy Framework all showed nothing at all, which reads as "we are not
watching this" rather than "nothing has changed".

This adapter reads the other half: the current publication of record for each watched
NIST obligation, with its release date. A revision replacing it is a real change
signal, and the release date is what puts these on the feed in the right place.

The series index pages are fully server-rendered and their rows are id-anchored
(`pub-number-191`, `pub-title-link-191`, `pub-release-date-191`), so parsing is exact
rather than positional. No JSON feed exists for finals: /CSRC/media/feeds/pubs/ serves
drafts-open-for-comment.json and nothing else, and every /api/ path 404s.

Only the watched numbers are kept. The SP 800 index alone lists 195 final publications
and the catalog tracks three of them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
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

BASE = "https://csrc.nist.gov"
# series slug -> index URL. AI 100-1 has no series index (/publications/ai is a 404),
# so the AI RMF stays on the drafts feed and the title linker.
SERIES_INDEX = {
    "sp800": f"{BASE}/publications/sp800",
    "cswp": f"{BASE}/publications/cswp",
    "fips": f"{BASE}/publications/fips",
}

# (series slug, publication number as CSRC prints it) -> obligation. The number column
# carries the revision too ("800-53 Rev. 5"), so this matches on its leading id.
WATCHED: dict[tuple[str, str], str] = {
    ("sp800", "800-53"): "nist-800-53",
    ("sp800", "800-171"): "nist-800-171",
    ("sp800", "800-63"): "nist-800-63",
    ("fips", "140-3"): "fips-140-3",
    ("cswp", "29"): "nist-csf",  # CSF 2.0 publishes as CSWP 29
    ("cswp", "10"): "nist-privacy-framework",
}

# One results row, anchored on the shared numeric id so the columns cannot drift apart.
_ROW_RE = re.compile(
    r'id="pub-number-(?P<n>\d+)">(?P<number>[^<]+)<'
    r'.*?href="(?P<href>/pubs/[^"]+)" id="pub-title-link-(?P=n)">(?P<title>[^<]*)<'
    r'.*?id="pub-status-(?P=n)">\s*(?P<status>[^<\s]+)\s*<'
    r'.*?id="pub-release-date-(?P=n)">\s*(?P<released>[\d/]+)\s*<',
    re.S,
)
# "800-53 Rev. 5" -> ("800-53", "Rev. 5"); "29" -> ("29", ""). The optional letter suffix
# is what keeps the companions out: 800-53A, 800-53B and 800-171A parse to their own ids
# and so never match a watched number.
_NUMBER_RE = re.compile(r"^\s*(?P<id>[0-9]+(?:-[0-9A-Za-z]+)?)\s*(?P<rev>.*?)\s*$")
# NIST names revisions two ways: "800-53 Rev. 5" and "800-63-4". Both mean revision N,
# and the catalog writes both as "Rev. N", so normalise to that or say nothing.
_REV_RE = re.compile(r"(?:Rev\.?\s*|^-)(\d+)", re.IGNORECASE)


@register
class NistPubsAdapter(SourceAdapter):
    """Current NIST publications of record for the watched obligations."""

    name = "nist_pubs"
    jurisdiction = "US-Federal"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        for series, url in SERIES_INDEX.items():
            resp = ctx.client.get(url)
            resp.raise_for_status()
            yield RawDocument(
                url=url,
                content=resp.content,
                content_type="text/html",
                http_status=resp.status_code,
                http_headers=dict(resp.headers),
                meta={"series": series},
            )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        series = raw.meta.get("series")
        if not series:
            return
        html = raw.content.decode("utf-8", errors="replace")
        for match in _ROW_RE.finditer(html):
            number_match = _NUMBER_RE.match(match.group("number"))
            if number_match is None:
                continue
            base_number = number_match.group("id")
            slug = WATCHED.get((series, base_number))
            if slug is None:
                continue
            released = _parse_date(match.group("released"))
            rev_match = _REV_RE.search(number_match.group("rev").strip())
            revision = f"Rev. {rev_match.group(1)}" if rev_match else ""
            label = f"{_LABELS[series]} {match.group('number').strip()}"
            yield NormalizedItem(
                source_system=self.name,
                # the publication page is the document's identity, and it changes when
                # NIST issues a revision — which is exactly when we want a new row
                external_key=("nist_pub", match.group("href")),
                jurisdiction=self.jurisdiction,
                title=f"{label}: {match.group('title').strip()}",
                url=BASE + match.group("href"),
                native_status=match.group("status").strip().lower(),
                track="final",
                obligation_slug=slug,
                published_at=released,
                dates=(
                    [NormalizedDate(DateType.effective, released, Confidence.published_firm)]
                    if released
                    else []
                ),
                native_meta={
                    "series": _LABELS[series],
                    "number": base_number,
                    "revision": revision,
                    # only a revision we actually read is a version claim; a first
                    # edition (CSWP 29, FIPS 140-3) carries its version in its title,
                    # and guessing one from the number would propose a bogus bump
                    **({"published_version": revision} if revision else {}),
                },
            )


_LABELS = {"sp800": "SP", "cswp": "CSWP", "fips": "FIPS"}


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None
