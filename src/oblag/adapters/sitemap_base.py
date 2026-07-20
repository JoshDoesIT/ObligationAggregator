"""Shared sitemap ingestion: bodies with no feed or API almost always still publish a
sitemap.xml whose URL slugs carry formal signals (new exposure drafts, version-release
announcements). New matching slugs → item_created events; that appearance is the signal."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime

from defusedxml import ElementTree

from oblag.adapters.base import FetchContext, RawDocument, SourceAdapter

_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


class SitemapAdapter(SourceAdapter):
    """Base: fetch one sitemap.xml and yield (url, lastmod) pairs to a subclass filter."""

    sitemap_url: str

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        resp = ctx.client.get(self.sitemap_url, headers={"Accept": "application/xml"})
        resp.raise_for_status()
        yield RawDocument(
            url=self.sitemap_url,
            content=resp.content,
            content_type="application/xml",
            http_status=resp.status_code,
            http_headers=dict(resp.headers),
            # normalize() has no FetchContext, so the incremental window rides on the
            # raw doc. Without it every page in the sitemap became an item on every
            # run (observed live: exposure drafts back to 2022 ingested as 'proposed').
            meta={"since": ctx.since.date().isoformat()} if ctx.since else {},
        )

    def iter_urls(self, raw: RawDocument) -> Iterable[tuple[str, date | None]]:
        """(url, lastmod) pairs, filtered to lastmod >= the run's incremental window.
        Entries with no lastmod pass through — a subclass can't know they're old."""
        since_str = raw.meta.get("since")
        since = date.fromisoformat(since_str) if since_str else None
        for loc, lastmod in self._iter_all_urls(raw):
            if since and lastmod and lastmod < since:
                continue
            yield loc, lastmod

    def _iter_all_urls(self, raw: RawDocument) -> Iterable[tuple[str, date | None]]:
        try:
            root = ElementTree.fromstring(raw.content)
        except ElementTree.ParseError:
            # Real-world sitemaps ship malformed XML (observed live: AICPA's contains a
            # raw unescaped '&' in a slug). Degrade to tolerant regex extraction rather
            # than losing the whole file to one bad entity.
            yield from self._iter_urls_tolerant(raw.content)
            return
        for url_el in root.iter(f"{_NS}url"):
            loc = (url_el.findtext(f"{_NS}loc") or "").strip()
            if not loc:
                continue
            lastmod = _parse_lastmod(url_el.findtext(f"{_NS}lastmod"))
            yield loc, lastmod

    @staticmethod
    def _iter_urls_tolerant(content: bytes) -> Iterable[tuple[str, date | None]]:
        text = content.decode("utf-8", errors="replace")
        for block in re.finditer(r"<url>(.*?)</url>", text, re.S):
            loc_match = re.search(r"<loc>\s*([^<]+?)\s*</loc>", block.group(1))
            if not loc_match:
                continue
            mod_match = re.search(r"<lastmod>\s*([^<]+?)\s*</lastmod>", block.group(1))
            yield loc_match.group(1), _parse_lastmod(mod_match.group(1) if mod_match else None)


def _parse_lastmod(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


def slug_to_title(url: str) -> str:
    """'…/ethics-exposure-draft-proposed-revised-interpretation-tax-services' →
    'Ethics exposure draft proposed revised interpretation tax services'."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    words = re.sub(r"[-_]+", " ", slug).strip()
    return (words[:1].upper() + words[1:]) if words else url
