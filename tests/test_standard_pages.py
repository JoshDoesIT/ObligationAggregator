"""Standards whose only publication surface is one page."""

from __future__ import annotations

from datetime import date

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.standard_pages import WATCHED, StandardPagesAdapter
from oblag.core.statemap import compute_state
from oblag.db.models import ItemState


def _items(page_key: str, fixture: str):
    raw = RawDocument(
        url="https://test",
        content=load_fixture("standard_pages", fixture),
        content_type="text/html",
        meta={"page": page_key},
    )
    return list(StandardPagesAdapter().normalize(raw))


def test_cis_controls_version_comes_off_its_own_page():
    """CIS has no release feed — the strict "CIS Controls vX" filter on its blog
    correctly matched nothing, so the obligation showed nothing at all."""
    item = _items("cis-controls", "cis.html")[0]
    assert item.obligation_slug == "cis-controls"
    assert item.title == "CIS Critical Security Controls v8.1"
    assert item.native_meta["published_version"] == "8.1"


def _nydfs(resolved: str, last_modified: str | None = None):
    raw = RawDocument(
        url="https://www.dfs.ny.gov/cybersecurity/23-NYCRR-Part-500",
        content=b"%PDF-1.7 binary regulation text",
        content_type="application/pdf",
        http_headers={"last-modified": last_modified} if last_modified else {},
        meta={"page": "nydfs-500", "resolved_url": resolved},
    )
    return list(StandardPagesAdapter().normalize(raw))


_PART_500_PDF = (
    "https://www.dfs.ny.gov/system/files/documents/2026/07/"
    "NYCRR-part-500-Cybersecurity-Regulation.pdf"
)


def test_nydfs_currency_is_read_from_where_the_link_points():
    """DFS rebuilt its site and deleted the sentence this used to parse ("On November 1,
    2023, DFS announced amendments to Cybersecurity Regulation"). The word "amendment"
    now appears on none of its cybersecurity pages — the regulation is served as one
    consolidated PDF under a dated CMS path. So the signal is the resolved link: when
    DFS reissues the text, the path moves and this row changes."""
    (item,) = _nydfs(_PART_500_PDF, "Thu, 16 Jul 2026 17:29:15 GMT")
    assert item.obligation_slug == "nydfs-500"
    assert item.title == "23 NYCRR Part 500: regulation text posted 16 July 2026"
    assert item.published_at == date(2026, 7, 16)
    # the dated path is only ever a month, so it is the identity marker and the
    # document's own timestamp supplies the day
    assert item.native_meta["published_version"] == "2026/07"


def test_a_cms_timestamp_is_never_asserted_as_an_effective_date():
    """Last-Modified says the body rewrote the file, which is not a claim about when the
    regulation takes effect. Publishing it as `effective` would invent a source statement."""
    (item,) = _nydfs(_PART_500_PDF, "Thu, 16 Jul 2026 17:29:15 GMT")
    assert item.dates == []
    # and an unparseable header costs the date, not the item
    (bare,) = _nydfs(_PART_500_PDF, "not a date")
    assert bare.published_at is None
    assert bare.title == "23 NYCRR Part 500: regulation text posted 2026/07"


def test_another_document_in_the_same_dated_folder_is_not_the_regulation():
    """DFS files everything under /documents/YYYY/MM/, so the date alone proves nothing.
    The filename has to carry Part 500 as well."""
    other = "https://www.dfs.ny.gov/system/files/documents/2026/07/il20260716-vishing.pdf"
    assert _nydfs(other) == []
    assert _nydfs("https://www.dfs.ny.gov/cybersecurity") == []


def test_csa_ccm_carries_its_version_and_release_date():
    item = _items("csa-ccm", "csa.html")[0]
    assert item.title == "CSA Cloud Controls Matrix v4.1"
    assert item.published_at == date(2026, 1, 27)
    assert item.native_meta["published_version"] == "4.1"


def test_a_page_that_says_it_is_superseded_records_nothing():
    """CSA leaves old artifact pages up with a notice at the top. Without the guard we
    would have published CCM v4.0 (2021) as current while v4.1 (2026) was out — the
    exact staleness this adapter exists to catch."""
    assert _items("csa-ccm", "csa_superseded.html") == []


def test_a_bare_version_number_never_matches():
    """The CSA page carries ?ver=4.0.13 on a WordPress asset, and a loose v(\\d+\\.\\d+)
    matched that instead of the standard. Patterns anchor on the body's own words."""
    page = next(p for p in WATCHED if p.key == "csa-ccm")
    assert page.pattern.search("script.js?ver=4.0.13") is None
    assert page.pattern.search("CCM v9.9") is None


def test_one_row_per_page_so_a_new_version_updates_rather_than_stacks():
    keys = {p.key for p in WATCHED}
    for key, fixture in (("cis-controls", "cis.html"), ("csa-ccm", "csa.html")):
        item = _items(key, fixture)[0]
        assert item.external_key == ("watched_page", key)
    assert len(keys) == len(WATCHED)


def test_statemap():
    today = date(2026, 7, 27)
    assert compute_state("standard_pages", "current", {}, {}, today) is ItemState.effective
    assert compute_state("standard_pages", "other", {}, {}, today) is None


def test_an_unrecognisable_page_yields_nothing_rather_than_a_guess():
    for meta in ({"page": "cis-controls"}, {"page": "nope"}, {}):
        raw = RawDocument(url="t", content=b"<html>nothing here</html>", meta=meta)
        assert list(StandardPagesAdapter().normalize(raw)) == []


def test_a_variant_that_leads_with_an_old_version_does_not_win():
    """Two fetches of the same CIS URL minutes apart came back as different CDN
    variants, and one mentioned v7.1 before v8.1. First-match would have published a
    superseded edition as the current standard."""
    html = (
        b"<html><body><p>Upgrading from CIS Controls v7.1 to CIS Controls v8.1. "
        b"See the CIS Controls v8 mapping.</p></body></html>"
    )
    raw = RawDocument(url="t", content=html, meta={"page": "cis-controls"})
    (item,) = StandardPagesAdapter().normalize(raw)
    assert item.title == "CIS Critical Security Controls v8.1"


def test_a_companion_document_does_not_become_the_standards_version():
    """Observed live on 2026-07-29: CIS listed a white paper titled "CIS Controls v8.1.2
    AI Security Guidance Workbook" in its Information Hub, and taking the highest
    version made the row claim a Controls release that does not exist. The page says
    v8.1 seven times and v8.1.2 once."""
    html = (
        b"<html><body><p>CIS Controls v8.1 is the current release. Download CIS Controls "
        b"v8.1. Read about CIS Controls v8.1 mappings. CIS Controls v8.1 FAQ. "
        b"CIS Controls v8.1 poster. CIS Controls v8.1 guide. CIS Controls v8.1 change log."
        b"</p><aside>White Paper 07.27.2026 CIS Controls v8.1.2 AI Security Guidance "
        b"Workbook</aside></body></html>"
    )
    raw = RawDocument(url="t", content=html, meta={"page": "cis-controls"})
    (item,) = StandardPagesAdapter().normalize(raw)
    assert item.title == "CIS Critical Security Controls v8.1"


def test_the_ai_rmf_page_is_watched_because_no_series_index_carries_it():
    """AI 100-1 has no CSRC series index (/publications/ai is a 404), so nist_pubs
    cannot see it and this page is the only surface stating the version."""
    page = next(p for p in WATCHED if p.key == "nist-ai-rmf")
    assert page.obligation == "nist-ai-rmf"
    html = (
        b"<html><body><p>The AI RMF 1.0 is being revised. "
        b"Download the AI RMF 1.0.</p></body></html>"
    )
    raw = RawDocument(url="t", content=html, meta={"page": "nist-ai-rmf"})
    (item,) = StandardPagesAdapter().normalize(raw)
    assert item.title == "NIST AI Risk Management Framework 1.0"
    assert item.obligation_slug == "nist-ai-rmf"
    assert item.native_meta["published_version"] == "1.0"
