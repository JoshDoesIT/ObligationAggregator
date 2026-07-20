from __future__ import annotations

import re
from collections.abc import Iterable

from oblag.adapters import register
from oblag.adapters.base import NormalizedDate, NormalizedItem, RawDocument
from oblag.adapters.sitemap_base import SitemapAdapter, slug_to_title
from oblag.db.models import Confidence, DateType

# The exposure-drafts landing SPA is broken upstream (its getStaticLandingPage GraphQL
# query 500s server-side — captured live 2026-07-18) and the GraphQL API requires a
# browser session. The sitemap, however, lists every exposure-draft page directly.
_EXPOSURE_RE = re.compile(r"(?i)/[a-z-]*exposure-draft")
_SOC_RE = re.compile(r"(?i)trust-services|soc-2|soc2|attestation")


@register
class AicpaAdapter(SitemapAdapter):
    """AICPA exposure drafts via sitemap.xml — new exposure-draft slugs are the formal
    signal; comment deadlines (not in the sitemap) arrive via curated assert-date."""

    name = "aicpa"
    jurisdiction = "Global"
    sitemap_url = "https://www.aicpa-cima.com/sitemap.xml"

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        for loc, lastmod in self.iter_urls(raw):
            if not _EXPOSURE_RE.search(loc):
                continue
            dates = []
            if lastmod:
                dates.append(NormalizedDate(DateType.proposal_date, lastmod, Confidence.derived))
            yield NormalizedItem(
                source_system=self.name,
                external_key=("aicpa_page", loc),
                jurisdiction=self.jurisdiction,
                title=f"AICPA exposure draft: {slug_to_title(loc)}",
                url=loc,
                native_status="exposure_draft",
                track="proposed",
                dates=dates,
                obligation_slug="soc2" if _SOC_RE.search(loc) else None,
            )
