"""The PCI standards themselves, not just the RFCs announced about them."""

from __future__ import annotations

from datetime import date

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.pci_docs import PciDocsAdapter
from oblag.core.statemap import compute_state
from oblag.db.models import DateType, ItemState


def _items():
    raw = RawDocument(
        url="https://www.pcisecuritystandards.org/rssfeed/?type=document",
        content=load_fixture("pci_docs", "documents.xml"),
        content_type="application/rss+xml",
    )
    return list(PciDocsAdapter().normalize(raw))


def test_every_pci_obligation_in_the_catalog_gets_its_standard():
    """Six of these had never shown anything: the blog adapter only sees a standard
    while it is under consultation, and most PCI standards are not."""
    by_slug = {i.obligation_slug: i for i in _items()}
    for slug in (
        "pci-dss",
        "pci-3ds",
        "pci-mpoc",
        "pci-p2pe",
        "pci-pin",
        "pci-pts-poi",
        "pci-pts-hsm",
        "pci-tsp",
        "pci-secure-software",
        "pci-secure-slc",
        "pci-card-production",
    ):
        assert slug in by_slug, slug
        assert by_slug[slug].published_at is not None, slug


def test_only_documents_that_ARE_the_standard_are_kept():
    """The feed is 436 documents: guidance, programme papers, FAQs, SAQs, reporting
    templates, case studies. One per family is the standard."""
    items = _items()
    assert len(items) == 11, [i.title for i in items]
    for i in items:
        assert i.native_status == "standard"
        for noise in ("FAQ", "Summary of Changes", "Template", "Annex", "Matrix"):
            assert noise.lower() not in i.title.lower(), i.title


def test_a_shared_document_slug_still_resolves_to_the_standard():
    """MPoC's standard and its summary of changes carry the SAME document= slug, so the
    slug alone cannot tell them apart — the title has to."""
    mpoc = next(i for i in _items() if i.obligation_slug == "pci-mpoc")
    assert mpoc.title == "Mobile Payments on COTS Security and Test Requirements"
    assert mpoc.published_at == date(2024, 11, 25)


def test_the_document_slug_is_the_identity():
    """A standard keeps its document= slug across revisions, so a new edition updates
    the row and its date rather than stacking another row beside it."""
    items = _items()
    keys = [i.external_key for i in items]
    assert all(t == "pci_document" for t, _ in keys)
    assert len(set(keys)) == len(keys)


def test_dates_are_parsed_from_rfc822_pubdate():
    dss = next(i for i in _items() if i.obligation_slug == "pci-dss")
    assert dss.published_at == date(2024, 6, 11)
    effective = [d for d in dss.dates if d.date_type is DateType.effective]
    assert effective and effective[0].value == dss.published_at


def test_statemap():
    assert compute_state("pci_docs", "standard", {}, {}, date(2026, 7, 27)) is ItemState.effective
    assert compute_state("pci_docs", "something-new", {}, {}, date(2026, 7, 27)) is None


def test_malformed_feed_yields_nothing_rather_than_raising():
    for content in (b"<html>nope</html>", b"", b"<rss><channel></channel></rss>"):
        assert list(PciDocsAdapter().normalize(RawDocument(url="t", content=content))) == []


def test_an_unknown_document_never_files_itself_against_a_standard(db):
    """The allowlist is the point: a new document category must not quietly start
    creating rows for an obligation."""
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Brand New Thing</title>
    <description><![CDATA[ PCI DSS: Standard ]]></description>
    <category><![CDATA[ PCI DSS ]]></category>
    <link><![CDATA[ https://www.pcisecuritystandards.org/dl/?document=brand_new ]]></link>
    <pubDate>Wed, 01 Jul 2026 07:00:00 +0000</pubDate></item>
    </channel></rss>"""
    assert list(PciDocsAdapter().normalize(RawDocument(url="t", content=feed))) == []
