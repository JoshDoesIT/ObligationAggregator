from __future__ import annotations

import re
from collections.abc import Iterable

from oblag.adapters import register
from oblag.adapters.base import NormalizedItem, RawDocument
from oblag.adapters.sitemap_base import SitemapAdapter, slug_to_title

# Formal signals in HITRUST slugs (no feed exists; WP REST API disabled — probed):
#   press-releases/hitrust-announces-csf-v11.3.0-launch        → CSF version release
#   blog/…-release-of-version-11.4.0-of-the-hitrust-csf        → CSF version release
#   advisories/haa-2023-003-csf-v9.6.2-…                       → formal advisory
_VERSION_RE = re.compile(r"(?i)csf[-.]?v?(\d+(?:\.\d+)+)|version[-.](\d+(?:\.\d+)+)[-a-z]*hitrust")
_SECTION_RE = re.compile(r"hitrustalliance\.net/(press-releases|advisories|blog)/")
_ADVISORY_RE = re.compile(r"(?i)/advisories/(haa-\d{4}-\d{3})")


@register
class HitrustAdapter(SitemapAdapter):
    """HITRUST CSF version releases + formal advisories via sitemap.xml."""

    name = "hitrust"
    jurisdiction = "Global"
    sitemap_url = "https://hitrustalliance.net/sitemap.xml"

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        seen_versions: set[str] = set()
        for loc, _lastmod in self.iter_urls(raw):
            section = _SECTION_RE.search(loc)
            if not section:
                continue
            version_match = _VERSION_RE.search(loc)
            advisory_match = _ADVISORY_RE.search(loc)
            if version_match and section.group(1) in ("press-releases", "blog"):
                version = version_match.group(1) or version_match.group(2)
                if version in seen_versions:
                    continue
                seen_versions.add(version)
                yield NormalizedItem(
                    source_system=self.name,
                    external_key=("hitrust_release", version),
                    jurisdiction=self.jurisdiction,
                    title=f"HITRUST CSF v{version}",
                    url=loc,
                    native_status="release",
                    track="final",
                    obligation_slug="hitrust-csf",
                )
            elif advisory_match and version_match:
                # advisories only when tied to a CSF version (formal lifecycle signal,
                # e.g. version submission deadlines) — general advisories are noise
                # the slug starts with the advisory id — strip it so the title
                # doesn't read "HAA-2017-003: Haa 2017 003 interim assessment…"
                subject_slug = re.sub(
                    rf"(?i)^{advisory_match.group(1)}-?", "", loc.rstrip("/").rsplit("/", 1)[-1]
                )
                yield NormalizedItem(
                    source_system=self.name,
                    external_key=("hitrust_advisory", advisory_match.group(1)),
                    jurisdiction=self.jurisdiction,
                    title=f"HITRUST advisory {advisory_match.group(1).upper()}: "
                    f"{slug_to_title(subject_slug)}",
                    url=loc,
                    native_status="advisory",
                    track="final",
                    obligation_slug="hitrust-csf",
                )
