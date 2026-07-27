"""FedRAMP programme announcements, from the sitemap.

fedramp.gov has no feed: /news, /rss.xml, /feed.json and /documents all 404, and the
Rev 5 documents page states no version — only nav text. What it does publish is a
sitemap of 1116 URLs whose announcement slugs carry their own date
(`/2026-06-25-propelling-change-fedramp-launches-consolidated-rules.../`), with a
matching lastmod. Same shape AICPA and HITRUST already use.

Most of those announcements are programme news — a new leader, an RFQ, a shutdown
notice, governance updates. None of that is a change to the obligation, and spec 00
says a weak signal never becomes an item. So a slug has to name a thing that changes
what an agency or a CSP must actually do: a baseline, a revision, the rules, a policy,
a directive response. Everything else is dropped, which on the live sitemap turns 52
dated announcements into 17 items.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from oblag.adapters import register
from oblag.adapters.base import NormalizedDate, NormalizedItem, RawDocument
from oblag.adapters.sitemap_base import SitemapAdapter, slug_to_title
from oblag.db.models import Confidence, DateType

# "/2026-06-25-public-preview-consolidated-rules-2026/" -> date + slug. Casing is mixed
# upstream ("2020-09-01-updated-3PAO-obligations-and-performance-standards-document"), so
# match either and lowercase the slug for identity.
_DATED_RE = re.compile(r"/(?:archive/)?(?P<when>20\d\d-\d\d-\d\d)-(?P<slug>[A-Za-z0-9-]+)/?$")
# What makes an announcement an obligation change rather than programme news.
_SIGNAL_RE = re.compile(
    r"baseline|rev-?\d|revision|consolidated-rules|\brules\b|standard|requirement"
    r"|policy|guidance|directive|authorization-act|significant-change|cryptograph",
    re.IGNORECASE,
)
# Explicitly not: these words appear in slugs that also match a signal word above.
_NEWS_RE = re.compile(
    r"welcoming|new-leader|rfq|shutdown|anniversary|recap|fy\d\d|town-hall|webinar",
    re.IGNORECASE,
)


@register
class FedrampAdapter(SitemapAdapter):
    """FedRAMP announcements that change what the programme requires."""

    name = "fedramp"
    jurisdiction = "US-Federal"
    sitemap_url = "https://www.fedramp.gov/sitemap.xml"

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        for loc, _lastmod in self.iter_urls(raw):
            match = _DATED_RE.search(loc)
            if match is None:
                continue
            slug = match.group("slug").lower()
            if not _SIGNAL_RE.search(slug) or _NEWS_RE.search(slug):
                continue
            # The slug's own date is the announcement date and never drifts; lastmod
            # does (one live entry carries today's date on a 2025 announcement).
            announced = _date(match.group("when"))
            yield NormalizedItem(
                source_system=self.name,
                external_key=("fedramp_announcement", slug),
                jurisdiction=self.jurisdiction,
                title=_headline(slug),
                url=loc,
                native_status="announcement",
                track="final",
                obligation_slug="fedramp",
                published_at=announced,
                dates=(
                    [NormalizedDate(DateType.adopted, announced, Confidence.published_firm)]
                    if announced
                    else []
                ),
                native_meta={"slug": slug},
            )


def _date(value: str):
    from datetime import date

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# A slug is lowercase, so "fedramp-bod-23-02-guidance" reads as "Fedramp bod 23 02
# guidance" straight out of slug_to_title. These are the words that carry real casing.
_ACRONYMS = {
    "fedramp": "FedRAMP",
    "cisa": "CISA",
    "m": "M",  # OMB memoranda: "m-21-31"
    "bod": "BOD",
    "3pao": "3PAO",
    "rev": "Rev",
    "gsa": "GSA",
    "a2la": "A2LA",
    "fips": "FIPS",
    "jab": "JAB",
    "poam": "POA&M",
}


def _headline(slug: str) -> str:
    """'rev-5-baselines-have-been-approved' -> 'FedRAMP: Rev 5 baselines have been
    approved'. The prefix names the body, so a leading 'fedramp' in the slug is dropped
    rather than repeated, and hyphenated numbers keep their hyphen ('BOD 23-02')."""
    words = slug_to_title(slug).lower().split()
    while words and words[0] == "fedramp":
        words.pop(0)
    words = [_ACRONYMS.get(w, w) for w in words] or ["announcement"]
    if words[0].islower():
        words[0] = words[0].capitalize()
    return "FedRAMP: " + re.sub(r"(?<=[\dM]) (?=\d)", "-", " ".join(words))
