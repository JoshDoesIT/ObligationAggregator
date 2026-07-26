from __future__ import annotations

import html as html_lib
import re
from collections.abc import Iterable
from datetime import date

from oblag.adapters import register
from oblag.adapters.base import FetchContext, NormalizedItem, RawDocument
from oblag.adapters.sitemap_base import SitemapAdapter, slug_to_title

# Formal signals in HITRUST slugs (no feed exists; WP REST API disabled — probed):
#   press-releases/hitrust-announces-csf-v11.3.0-launch        → CSF version release
#   blog/…-release-of-version-11.4.0-of-the-hitrust-csf        → CSF version release
#   advisories/haa-2026-002-csf-version-11.8.0-release         → CSF version release
#   advisories/haa-2023-003-csf-v9.6.2-…                       → formal advisory
# Many advisory URLs are BARE ids ("advisories/haa-2025-001") with the subject only in
# the page <title> ("HAA 2025-001 HITRUST CSF Version 11.5.0 Release") — those pages
# are fetched individually (observed live: v11.5/11.6/11.7 releases hid behind bare
# ids and were silently missed by URL-pattern matching alone).
_VERSION_RE = re.compile(
    # "csf-v11.8.0", "csf v11.7", "csf-version-11.8.0", "csf-v9" (dotless family ref),
    # and the blog form "…version-11.4.0-of-the-hitrust-csf"
    r"(?i)csf[-.\s]?v(?:ersion)?[-.\s]?(\d+(?:\.\d+)*)"
    r"|version[-.\s](\d+(?:\.\d+)+)[-a-z\s]*hitrust"
)
_SECTION_RE = re.compile(r"hitrustalliance\.net/(press-releases|advisories|blog)/")
_ADVISORY_RE = re.compile(r"(?i)/advisories/(haa-\d{4}-\d{3})")
_BARE_ADVISORY_RE = re.compile(r"(?i)/advisories/(haa-\d{4}-\d{3})/?$")
_RELEASE_HINT_RE = re.compile(r"(?i)\brelease\b|\blaunch\b")
_TITLE_RE = re.compile(r"<title>\s*([^<]+?)\s*</title>", re.IGNORECASE)
# how many of the newest bare-id advisory pages to fetch per run (idempotent — the
# reducer dedups by advisory id; a small constant keeps runs cheap)
_BARE_FETCH_LIMIT = 12
# CSF majors below this are decommissioned history (v7/v8 advisories date to 2016);
# ingesting them now would only add noise to the hitrust-csf timeline
_MIN_MAJOR = 9


@register
class HitrustAdapter(SitemapAdapter):
    """HITRUST CSF version releases + formal advisories via sitemap.xml, plus targeted
    page fetches for bare-id advisory URLs whose subject only lives in the page title."""

    name = "hitrust"
    jurisdiction = "Global"
    sitemap_url = "https://hitrustalliance.net/sitemap.xml"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        sitemap_doc = None
        for doc in super().fetch_raw(ctx):
            sitemap_doc = doc
            yield doc
        if sitemap_doc is None:
            return
        # newest bare-id advisories regardless of lastmod: ids sort chronologically,
        # and the since-window must not hide releases announced before first ingest
        by_id: dict[str, date | None] = {}
        for loc, lastmod in self._iter_all_urls(sitemap_doc):
            if m := _BARE_ADVISORY_RE.search(loc):
                by_id[m.group(1).lower()] = lastmod
        for advisory_id in sorted(by_id, reverse=True)[:_BARE_FETCH_LIMIT]:
            url = f"https://hitrustalliance.net/advisories/{advisory_id}"
            resp = ctx.client.get(url, headers={"Accept": "text/html"}, follow_redirects=True)
            if resp.status_code != 200:
                continue
            lastmod = by_id[advisory_id]
            yield RawDocument(
                url=url,
                content=resp.content,
                content_type="text/html",
                http_status=resp.status_code,
                http_headers=dict(resp.headers),
                meta={
                    "advisory_id": advisory_id,
                    **({"lastmod": lastmod.isoformat()} if lastmod else {}),
                },
            )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        if raw.meta.get("advisory_id"):
            yield from self._normalize_advisory_page(raw)
            return
        seen_versions: set[str] = set()
        # Deliberately NOT windowed by lastmod (unlike other sitemap adapters):
        # releases and advisories are final-track HISTORY, not "appeared = new
        # proposal" signals, so re-reading old entries is harmless — and the window
        # permanently hid any release page last modified before it (observed live:
        # the v11.8.0 release advisory, lastmod 8 May, invisible to every 3/10-day
        # scheduled run). The reducer makes re-yields idempotent.
        for loc, lastmod in self._iter_all_urls(raw):
            section = _SECTION_RE.search(loc)
            if not section:
                continue
            version_match = _VERSION_RE.search(loc)
            advisory_match = _ADVISORY_RE.search(loc)
            version = (version_match.group(1) or version_match.group(2)) if version_match else None
            if version and _too_old(version):
                continue
            slug = loc.rstrip("/").rsplit("/", 1)[-1]
            if version and (
                section.group(1) in ("press-releases", "blog")
                or (advisory_match and _RELEASE_HINT_RE.search(slug))
            ):
                # a version-release announcement — whether posted as a press release,
                # a blog post, or an advisory slugged "…-release"
                if version in seen_versions:
                    continue
                seen_versions.add(version)
                yield self._release_item(version, loc, lastmod)
            elif advisory_match and version:
                # advisories only when tied to a CSF version (formal lifecycle signal,
                # e.g. version submission deadlines) — general advisories are noise
                yield self._advisory_item(advisory_match.group(1), loc, lastmod)

    def _normalize_advisory_page(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        """Classify a bare-id advisory from its page title, e.g.
        'HAA 2025-001 HITRUST CSF Version 11.5.0 Release'."""
        advisory_id = raw.meta["advisory_id"]
        lastmod = date.fromisoformat(raw.meta["lastmod"]) if raw.meta.get("lastmod") else None
        html = raw.content.decode("utf-8", errors="replace")
        title_match = _TITLE_RE.search(html)
        if not title_match:
            return
        title = _clean_text(title_match.group(1))
        version_match = _VERSION_RE.search(title)
        if not version_match:
            return  # advisory without a CSF version subject — noise, skip
        version = version_match.group(1) or version_match.group(2)
        if _too_old(version):
            return
        if _RELEASE_HINT_RE.search(title):
            yield self._release_item(version, raw.url, lastmod)
        else:
            subject = re.sub(rf"(?i)^haa\s*{advisory_id[4:]}\s*", "", title)
            subject = re.sub(r"^[\s:–—-]+", "", subject).strip()
            yield NormalizedItem(
                source_system=self.name,
                external_key=("hitrust_advisory", advisory_id),
                jurisdiction=self.jurisdiction,
                title=f"HITRUST advisory {advisory_id.upper()}: {subject or title}",
                url=raw.url,
                native_status="advisory",
                track="final",
                obligation_slug="hitrust-csf",
                published_at=lastmod,
            )

    def _release_item(self, version: str, url: str, lastmod: date | None) -> NormalizedItem:
        return NormalizedItem(
            source_system=self.name,
            external_key=("hitrust_release", version),
            jurisdiction=self.jurisdiction,
            title=f"HITRUST CSF v{version}",
            url=url,
            native_status="release",
            track="final",
            obligation_slug="hitrust-csf",
            native_meta={"published_version": version},
            published_at=lastmod,
        )

    def _advisory_item(self, advisory_id: str, loc: str, lastmod: date | None) -> NormalizedItem:
        # the slug starts with the advisory id — strip it so the title
        # doesn't read "HAA-2017-003: Haa 2017 003 interim assessment…"
        subject_slug = re.sub(rf"(?i)^{advisory_id}-?", "", loc.rstrip("/").rsplit("/", 1)[-1])
        return NormalizedItem(
            source_system=self.name,
            external_key=("hitrust_advisory", advisory_id),
            jurisdiction=self.jurisdiction,
            title=f"HITRUST advisory {advisory_id.upper()}: {slug_to_title(subject_slug)}",
            url=loc,
            native_status="advisory",
            track="final",
            obligation_slug="hitrust-csf",
            published_at=lastmod,
        )


def _too_old(version: str) -> bool:
    try:
        return int(version.split(".")[0]) < _MIN_MAJOR
    except ValueError:
        return False


def _clean_text(text: str) -> str:
    """Page titles arrive with HTML entities and non-breaking spaces
    ('HITRUST&nbsp;CSF v11.5' — observed live in prod item titles)."""
    return re.sub(r"\s+", " ", html_lib.unescape(text).replace("\xa0", " ")).strip()
