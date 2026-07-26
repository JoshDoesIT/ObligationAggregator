"""ISO/IEC standard editions and amendments, read from IEC — the co-publisher.

iso.org is unreadable to us and will stay that way. www.iso.org sits behind a
Cloudflare managed challenge (our User-Agent and a full Chrome one both get 403, and
even /robots.txt returns the challenge page), and standards.iso.org — which IS
reachable — publishes `User-agent: * / Disallow: /`. That is ISO stating a crawl
policy in the standard way, so the answer there is no, not "not yet". obp.iso.org is
an Angular shell whose /api/* needs a login.

The way through is that these are JOINT standards. ISO/IEC 27001, 27002, 27017, 27018,
27701 and 42001 are all ISO/IEC JTC 1/SC 27 publications, so IEC publishes them too
and is an equally authoritative source of record. webstore.iec.ch serves an empty
robots.txt (no restriction) and its search is backed by a public JSON API, so this
needs no browser and runs fine on serverless.

Coverage measured against the live API, not assumed. Five of the seven watched
standards come back as current editions with real publication dates (27002, 42001,
27701, 27017, 27018), and 27001 comes back as its amendment, ISO/IEC 27001:2022/AMD1
:2024, a change to the most-watched standard in the catalog that nothing else had told
us about. Two gaps remain and stay operator-curated: ISO 22301 is ISO-only so IEC has
no listing at all, and IEC's store carries 27001's 2013 and 2005 editions but not the
2022 one. Both cases emit nothing rather than a stub, which leaves the curated rows
untouched instead of overwriting good data with a placeholder.

Metadata only, never standard text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date

from oblag.adapters import register
from oblag.adapters.base import FetchContext, NormalizedItem, RawDocument, SourceAdapter
from oblag.db.session import session_scope

SEARCH_API = "https://webstore-search-api.iec.ch/api/search"
# "iso-27001" → "27001": the catalog slug carries the number IEC indexes on
_NUMBER_RE = re.compile(r"(\d{4,5})")
# Amendments and corrigenda are separate publications ABOUT the base standard, and are
# exactly the signal this adapter exists to catch — the 27001 search returns
# ISO/IEC 27001:2022/AMD1:2024, which no other source had told us about.
_SUPPLEMENT_RE = re.compile(r"/(AMD|COR)\d*", re.IGNORECASE)
# Handbooks and guides name a standard in their reference without being it
_NOT_THE_STANDARD_RE = re.compile(r"-(HBK|GUIDE)", re.IGNORECASE)


@register
class IsoCatalogAdapter(SourceAdapter):
    """Editions, amendments and publication dates for the watched ISO/IEC standards."""

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
            number_match = _NUMBER_RE.search(slug)
            if not number_match:
                continue
            number = number_match.group(1)
            body = json.dumps(
                {
                    "query": number,
                    "language": "en",
                    "mode": "FULL",
                    "perPage": 25,
                    "currentPage": 1,
                    # in-force publications only: superseded editions would otherwise
                    # come back alongside the current one and read as new filings
                    "validOnly": True,
                }
            ).encode()
            resp = ctx.client.post(
                SEARCH_API,
                content=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            resp.raise_for_status()
            yield RawDocument(
                url=SEARCH_API,
                content=resp.content,
                content_type="application/json",
                http_status=resp.status_code,
                http_headers=dict(resp.headers),
                meta={"obligation_slug": slug, "catalog_url": url, "number": number},
            )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        slug = raw.meta.get("obligation_slug")
        number = raw.meta.get("number")
        if not slug or not number:
            return
        try:
            payload = json.loads(raw.content.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return
        hits = ((payload.get("primary") or {}).get("hits") or {}).get("hits") or []

        # A number search is a substring search, so "27001" also returns 27001-HBK and
        # anything quoting it. Only references that ARE this standard survive.
        wanted = re.compile(rf"^(ISO/IEC|ISO)\s{number}(:|/|$)")
        for hit in hits:
            src = hit.get("_source") or {}
            reference = (src.get("reference") or "").strip()
            if not wanted.match(reference) or _NOT_THE_STANDARD_RE.search(reference):
                continue
            pub_id = str(src.get("id") or "")
            supplement = bool(_SUPPLEMENT_RE.search(reference))
            full_title = (src.get("title") or "").strip()
            yield NormalizedItem(
                source_system=self.name,
                # The base standard keeps the identity the iso.org-era rows already
                # carry, so switching source refreshes those rows instead of doubling
                # them. Supplements are their own publications and get their own key.
                external_key=(
                    ("iec_pub", pub_id)
                    if supplement
                    else ("iso_project", raw.meta.get("catalog_url") or slug)
                ),
                jurisdiction=self.jurisdiction,
                # A reference IS how a standard is cited, so it stands alone. An
                # amendment's reference does not say what it changed, so that one
                # carries the distinctive tail of the title too.
                title=f"{reference} {_tail(full_title)}".strip() if supplement else reference,
                url=f"https://webstore.iec.ch/en/publication/{pub_id}" if pub_id else None,
                abstract=(src.get("abstract") or "").strip() or full_title or None,
                native_status=(src.get("status") or "published").strip().lower(),
                track="final",
                obligation_slug=slug,
                native_meta={
                    "reference": reference,
                    "edition": str(src.get("edition") or ""),
                    "publication_date": str(src.get("publication_date") or ""),
                    "co_publisher": "IEC",
                },
                published_at=_parse_date(src.get("publication_date")),
            )


def _tail(title: str) -> str:
    """The distinctive last clause of an IEC title, which spells out its scope prefix
    first: "Information security ... - Requirements - Amendment 1: Climate action
    changes" → "Amendment 1: Climate action changes"."""
    return title.rsplit(" - ", 1)[-1].strip()


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
