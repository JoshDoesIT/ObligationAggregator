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


def test_nydfs_amendment_date_is_read_as_a_date_not_a_version():
    """23 NYCRR 500 has no version number; DFS states amendments as a dated sentence."""
    item = _items("nydfs-500", "nydfs.html")[0]
    assert item.obligation_slug == "nydfs-500"
    assert item.published_at == date(2023, 11, 1)
    assert "published_version" not in item.native_meta


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


def test_the_highest_version_on_the_page_wins_not_the_first_one_mentioned():
    """Observed live: two fetches of the same CIS URL minutes apart came back as
    different CDN variants, and one mentioned v7.1 before v8.1. First-match would have
    published a superseded edition as the current standard."""
    html = (
        b"<html><body><p>Upgrading from CIS Controls v7.1 to CIS Controls v8.1. "
        b"See the CIS Controls v8 mapping.</p></body></html>"
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
