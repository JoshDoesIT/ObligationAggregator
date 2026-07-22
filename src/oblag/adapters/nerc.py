from __future__ import annotations

import re
from collections.abc import Iterable

from oblag.adapters import register
from oblag.adapters.base import (
    FetchContext,
    NormalizedItem,
    RawDocument,
    SourceAdapter,
)

PAGE_URL = "https://www.nerc.com/standards/reliability-standards-under-development"

# Project links on the listing page carry number + full name in the slug, e.g.
# ".../2023-09-risk-management-for-third-party-cloud-services". The page's PROSE
# mentions of "Project 2025-03" are webinar/meeting copy — parsing them fabricated
# titles like "Breakout Session" (observed live).
_PROJECT_LINK_RE = re.compile(
    r"standards/reliability-standards-under-development/((\d{4})-(\d{2})-([a-z0-9.-]+))",
    re.IGNORECASE,
)
# The page lists EVERY reliability-standards project (frequency response, cold
# weather, relay loadability…) — only cyber/CIP work belongs on a security feed.
_CYBER_RE = re.compile(
    r"cip|cyber|internal-network-security|supply-chain|third-party-cloud", re.IGNORECASE
)
# Projects from before this floor are long-completed CIP work (v5 revisions, Order
# 706) — listing them as under development would be its own inaccuracy.
_MIN_YEAR = 2020

# Project pages embed a status block in page JSON:
#   "infoTitle":"Status","infoDescriptionHtml":"<p>Board adopted and filed with FERC</p>"
_STATUS_RE = re.compile(r'"infoTitle":"Status","infoDescriptionHtml":"(.*?)"[,}]')
_TAG_RE = re.compile(r"<[^>]+>")


@register
class NercAdapter(SourceAdapter):
    """NERC standards-under-development: CIP/cyber projects only, with per-project
    status parsed from each project page's embedded JSON. Ballot/comment dates live in
    SharePoint documents — tracked via curated assertions when needed."""

    name = "nerc"
    jurisdiction = "US-Federal"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        resp = ctx.client.get(PAGE_URL, headers={"Accept": "text/html"}, follow_redirects=True)
        resp.raise_for_status()
        listing = resp.content.decode("utf-8", errors="replace")
        projects = _cyber_projects(listing)
        yield RawDocument(
            url=str(resp.url),
            content=resp.content,
            content_type="text/html",
            http_status=resp.status_code,
            http_headers=dict(resp.headers),
            meta={"kind": "listing", "projects_found": str(len(projects))},
        )
        for number, slug in projects:
            proj_url = (
                f"https://www.nerc.com/standards/reliability-standards-under-development/{slug}"
            )
            proj = ctx.client.get(proj_url, headers={"Accept": "text/html"}, follow_redirects=True)
            if proj.status_code != 200:
                continue
            yield RawDocument(
                url=str(proj.url),
                content=proj.content,
                content_type="text/html",
                http_status=proj.status_code,
                http_headers=dict(proj.headers),
                meta={"kind": "project", "slug": slug, "number": number},
            )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        if raw.meta.get("kind") == "listing":
            if raw.meta.get("projects_found") == "0":
                yield NormalizedItem(
                    source_system=self.name,
                    external_key=("nerc_page", PAGE_URL),
                    jurisdiction=self.jurisdiction,
                    title="NERC standards under development",
                    url=PAGE_URL,
                    native_status="unknown",
                    track="default",
                    obligation_slug="nerc-cip",
                    anomalies=["no cyber project links parsed; page restructured?"],
                )
            return
        if raw.meta.get("kind") != "project":
            return
        slug = raw.meta["slug"]
        number = raw.meta["number"]
        import html as htmllib

        page = raw.content.decode("utf-8", errors="replace")
        status_match = _STATUS_RE.search(page)
        status = "unknown"
        if status_match:
            # the JSON-embedded value carries escaped newlines, tags and entities
            text = status_match.group(1).replace("\\n", " ")
            status = htmllib.unescape(_TAG_RE.sub("", text)).replace("\xa0", " ").strip()
        anomalies = [] if status != "unknown" else [f"no status block on project page {slug}"]
        yield NormalizedItem(
            source_system=self.name,
            external_key=("nerc_project", number),
            jurisdiction=self.jurisdiction,
            title=f"NERC Project {number}: {_slug_name(slug, number)}",
            url=raw.url,
            native_status=status,
            track="proposed",
            obligation_slug="nerc-cip",
            native_meta={"project_slug": slug},
            anomalies=anomalies,
        )


def _cyber_projects(listing_html: str) -> list[tuple[str, str]]:
    """(project-number, slug) for recent cyber/CIP projects, deduplicated."""
    seen: dict[str, str] = {}
    for m in _PROJECT_LINK_RE.finditer(listing_html):
        slug, year, seq = m.group(1).lower(), int(m.group(2)), m.group(3)
        if year < _MIN_YEAR or not _CYBER_RE.search(slug):
            continue
        seen.setdefault(f"{year}-{seq}", slug)
    return sorted(seen.items())


def _slug_name(slug: str, number: str) -> str:
    tail = slug[len(number) :].strip("-")
    words = re.sub(r"-+", " ", tail).strip()
    name = words[:1].upper() + words[1:]
    # keep recognizable tokens readable ("cip 008" → "CIP-008", "insm" → "INSM")
    name = re.sub(r"\bcip (\d{3})\b", lambda m: f"CIP-{m.group(1)}", name, flags=re.IGNORECASE)
    return re.sub(
        r"\b(cip|insm|ferc|bes)\b", lambda m: m.group(1).upper(), name, flags=re.IGNORECASE
    )
