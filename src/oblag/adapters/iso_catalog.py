from __future__ import annotations

import re
from collections.abc import Iterable

from oblag.adapters import register
from oblag.adapters.base import FetchContext, NormalizedItem, RawDocument, SourceAdapter
from oblag.db.session import session_scope

_TITLE_RE = re.compile(r"<title>\s*([^<]+?)\s*(?:</title>| - )", re.IGNORECASE)
_STAGE_RE = re.compile(r"Stage:?\s*(?:</[^>]+>[^0-9]{0,200}?)?(\d{2}\.\d{2})", re.S)
_PUBDATE_RE = re.compile(r"[Pp]ublication date[^0-9]{0,80}(\d{4}(?:-\d{2})?)")
_EDITION_RE = re.compile(r"Edition\s*:?\s*</[^>]*>\s*[^0-9]{0,40}(\d+)", re.S)


@register
class IsoCatalogAdapter(SourceAdapter):
    """iso.org catalog pages for watched standards (obligations with an iso.org URL).

    Tracks harmonized stage codes + edition metadata only — never standard text."""

    name = "iso_catalog"
    jurisdiction = "Global"

    def _watched(self, ctx: FetchContext) -> list[tuple[str, str]]:
        """(obligation_slug, catalog_url) pairs from params or the obligation catalog."""
        if ctx.params.get("standards"):
            return list(ctx.params["standards"])
        from oblag.db.models import Obligation

        with session_scope() as session:
            rows = (
                session.query(Obligation.slug, Obligation.canonical_url)
                .filter(Obligation.canonical_url.like("%iso.org%"))
                .all()
            )
        return [(slug, url) for slug, url in rows if url]

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        for slug, url in self._watched(ctx):
            resp = ctx.client.get(url, headers={"Accept": "text/html"})
            resp.raise_for_status()
            yield RawDocument(
                url=str(resp.url),
                content=resp.content,
                content_type="text/html",
                http_status=resp.status_code,
                http_headers=dict(resp.headers),
                meta={"obligation_slug": slug, "catalog_url": url},
            )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        slug = raw.meta.get("obligation_slug")
        if not slug:
            return
        html = raw.content.decode("utf-8", errors="replace")
        anomalies: list[str] = []

        stage_match = _STAGE_RE.search(html)
        stage = stage_match.group(1) if stage_match else "unknown"
        if stage == "unknown":
            anomalies.append(f"could not parse ISO stage code for {slug}")
        elif stage.startswith("95"):
            # The tracked page is an edition-pinned URL whose edition was withdrawn —
            # a NEW edition exists that this URL no longer shows. Without this alert
            # the new edition (and its year) would silently go untracked.
            anomalies.append(
                f"ISO page for {slug} reports stage {stage} (edition withdrawn/replaced): "
                "a newer edition exists — update the obligation's canonical_url to the "
                "new edition page"
            )

        title_match = _TITLE_RE.search(html)
        title = title_match.group(1).strip() if title_match else f"ISO catalog: {slug}"
        edition = m.group(1) if (m := _EDITION_RE.search(html)) else ""
        pubdate = m.group(1) if (m := _PUBDATE_RE.search(html)) else ""

        yield NormalizedItem(
            source_system=self.name,
            external_key=("iso_project", raw.meta.get("catalog_url") or slug),
            jurisdiction=self.jurisdiction,
            title=title,
            url=raw.url,
            native_status=stage,
            track="default",
            obligation_slug=slug,
            native_meta={"edition": edition, "publication_date": pubdate},
            anomalies=anomalies,
        )
