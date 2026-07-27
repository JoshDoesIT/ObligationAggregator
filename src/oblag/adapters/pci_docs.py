"""The PCI standards themselves, from the SSC's own document library feed.

pci_ssc reads the PCI Perspectives blog for RFC announcements, so a PCI standard only
appeared while it was under consultation. Six of the twelve PCI obligations in the
catalog had never shown anything at all: 3DS, MPoC, P2PE, PIN, PTS POI and TSP.

The document library at /document_library/ is a nav menu — the rows load client-side
and the documents API behind it answers 403. But the same site publishes
/rssfeed/?type=document, a 436-entry feed of every document the SSC has released, each
with a publication date, a category, a document type and a stable `document=` slug in
its link. That slug is the identity a standard keeps across revisions, so it is the
external key.

Zero-noise by construction, the same posture as the rest of the copyrighted layer: the
feed is 84 guidance documents, 91 programme/certification papers, FAQs, SAQs, reporting
templates and case studies, and none of that is an obligation change. Only documents
typed "Standard" AND named in DOCUMENT_OBLIGATIONS below become items, so a new
document category can never quietly start filing rows against a standard.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from defusedxml import ElementTree as DefusedET

from oblag.adapters import register
from oblag.adapters.base import (
    FetchContext,
    NormalizedDate,
    NormalizedItem,
    RawDocument,
    SourceAdapter,
)
from oblag.db.models import Confidence, DateType

FEED_URL = "https://www.pcisecuritystandards.org/rssfeed/?type=document"

# `document=` slug -> catalog obligation. Curated deliberately: the SSC publishes
# several documents per family (a standard, its summary of changes, its FAQs, its
# reporting template) and only one of them IS the standard.
DOCUMENT_OBLIGATIONS: dict[str, str] = {
    "pci_dss": "pci-dss",
    "3ds_standard": "pci-3ds",
    "mpocsectest": "pci-mpoc",
    "p2pe_solution_requirements": "pci-p2pe",
    "pcipinpin__sec_req_pdf": "pci-pin",
    "pci_pts_poi_sr_2": "pci-pts-poi",
    "pci_hsm_security_requirements": "pci-pts-hsm",
    "pci_tsp_requirements": "pci-tsp",
    "sec_sware_reqs_procs": "pci-secure-software",
    "sec_slc_std": "pci-secure-slc",
    "pci_card_production__prov_logical_security_requirements": "pci-card-production",
}
# The MPoC standard and its summary of changes share one `document=` slug, so the slug
# alone is not enough to tell them apart. These words never appear in a standard's own
# title and always appear in its satellites.
_NOT_THE_STANDARD = re.compile(
    r"summary of changes|technical faq|\bfaqs?\b|annex|data matrix|template|questionnaire",
    re.IGNORECASE,
)
_DOCUMENT_SLUG_RE = re.compile(r"[?&]document=([^&\s\]]+)")


@register
class PciDocsAdapter(SourceAdapter):
    """Current PCI SSC standards, dated, one row per standard."""

    name = "pci_docs"
    jurisdiction = "Global"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        resp = ctx.client.get(FEED_URL)
        resp.raise_for_status()
        yield RawDocument(
            url=FEED_URL,
            content=resp.content,
            content_type="application/rss+xml",
            http_status=resp.status_code,
            http_headers=dict(resp.headers),
        )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        try:
            root = DefusedET.fromstring(raw.content.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 — a malformed feed is skipped, never fatal
            return
        for entry in root.iter("item"):
            title = _text(entry, "title")
            link = _text(entry, "link")
            doc_type = _text(entry, "description").rsplit(":", 1)[-1].strip()
            if not title or doc_type != "Standard" or _NOT_THE_STANDARD.search(title):
                continue
            slug_match = _DOCUMENT_SLUG_RE.search(link)
            if slug_match is None:
                continue
            document = slug_match.group(1)
            obligation = DOCUMENT_OBLIGATIONS.get(document)
            if obligation is None:
                continue
            released = _parse_date(_text(entry, "pubDate"))
            yield NormalizedItem(
                source_system=self.name,
                # the document slug is what a standard keeps across revisions, so a new
                # edition updates this row and its published_at rather than piling up
                external_key=("pci_document", document),
                jurisdiction=self.jurisdiction,
                title=title,
                url=link or None,
                native_status="standard",
                track="final",
                obligation_slug=obligation,
                published_at=released,
                dates=(
                    [NormalizedDate(DateType.effective, released, Confidence.published_firm)]
                    if released
                    else []
                ),
                native_meta={"category": _text(entry, "category"), "document": document},
            )


def _text(entry, tag: str) -> str:
    node = entry.find(tag)
    return (node.text or "").strip() if node is not None else ""


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        try:
            return datetime.strptime(value[:16].strip(), "%a, %d %b %Y").date()
        except ValueError:
            return None
