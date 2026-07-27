"""NIST publications of record: the half the drafts feed cannot see."""

from __future__ import annotations

from datetime import date

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.nist_pubs import NistPubsAdapter
from oblag.core.statemap import compute_state
from oblag.db.models import DateType, ItemState


def _items(series: str):
    raw = RawDocument(
        url=f"https://csrc.nist.gov/publications/{series}",
        content=load_fixture("nist_pubs", f"{series}.html"),
        content_type="text/html",
        meta={"series": series},
    )
    return list(NistPubsAdapter().normalize(raw))


def test_only_the_watched_publications_are_kept():
    """The SP 800 index lists 195 finals and the catalog tracks three of them. An
    adapter that ingested the lot would bury the feed."""
    items = _items("sp800")
    assert {i.obligation_slug for i in items} == {
        "nist-800-53",
        "nist-800-171",
        "nist-800-63",
    }
    assert len(items) < 20, "watched numbers only, not the whole series"


def test_a_publication_carries_its_release_date_and_revision():
    item = next(i for i in _items("sp800") if i.obligation_slug == "nist-800-53")
    assert item.title.startswith("SP 800-53")
    assert item.published_at is not None
    assert item.native_meta["series"] == "SP"
    assert item.native_meta["number"] == "800-53"
    assert item.url.startswith("https://csrc.nist.gov/pubs/sp/800/53/")
    effective = [d for d in item.dates if d.date_type is DateType.effective]
    assert effective and effective[0].value == item.published_at


def test_the_framework_and_privacy_framework_come_from_cswp():
    """CSF 2.0 publishes as CSWP 29 and the Privacy Framework as CSWP 10 — neither is
    an SP, so a series-blind number match would have missed both."""
    by_slug = {i.obligation_slug: i for i in _items("cswp")}
    assert by_slug["nist-csf"].published_at == date(2024, 2, 26)
    assert "Cybersecurity Framework" in by_slug["nist-csf"].title
    assert by_slug["nist-privacy-framework"].published_at == date(2020, 1, 16)


def test_fips_140_3_is_found_in_its_own_series():
    item = next(i for i in _items("fips") if i.obligation_slug == "fips-140-3")
    assert item.title.startswith("FIPS 140-3")
    assert item.published_at is not None


def test_the_publication_page_is_the_identity():
    """A revision gets a new /pubs/ path, so keying on it means a revision creates a
    new row rather than silently overwriting the edition it replaced."""
    items = _items("sp800")
    keys = [i.external_key for i in items]
    assert all(t == "nist_pub" for t, _ in keys)
    assert len(set(keys)) == len(keys)


def test_statemap():
    for native, expected in (
        ("final", ItemState.effective),
        ("withdrawn", ItemState.withdrawn),
        ("draft", ItemState.proposed),
    ):
        assert compute_state("nist_pubs", native, {}, {}, date(2026, 7, 27)) is expected
    assert compute_state("nist_pubs", "something-new", {}, {}, date(2026, 7, 27)) is None


def test_malformed_page_yields_nothing_rather_than_raising():
    raw = RawDocument(url="https://t", content=b"<html>nope</html>", meta={"series": "sp800"})
    assert list(NistPubsAdapter().normalize(raw)) == []
    # a page with no series in meta is unattributable, so it produces nothing
    assert list(NistPubsAdapter().normalize(RawDocument(url="https://t", content=b"x"))) == []


def test_a_nist_revision_advances_the_catalog_version(db):
    from oblag.catalog import seed_obligations
    from oblag.core.reducer import reduce_item
    from oblag.db.models import Obligation
    from oblag.versionsuggest import auto_apply

    seed_obligations(db)
    db.query(Obligation).filter_by(slug="nist-800-53").update(
        {Obligation.current_version: "Rev. 4"}
    )
    db.commit()
    for item in _items("sp800"):
        reduce_item(db, item, today=date(2026, 7, 27))
    db.commit()
    auto_apply(db)
    db.expire_all()
    # the catalog writes NIST revisions as "Rev. N", and so does the adapter
    assert db.query(Obligation).filter_by(slug="nist-800-53").one().effective_version == "Rev. 5"


def test_both_of_nists_revision_spellings_normalise(db):
    """SP 800-53 is numbered "800-53 Rev. 5" and SP 800-63 is "800-63-4". Both mean
    revision N, and the catalog writes both as "Rev. N"."""
    by_slug = {i.obligation_slug: i for i in _items("sp800")}
    assert by_slug["nist-800-53"].native_meta["revision"] == "Rev. 5"
    assert by_slug["nist-800-63"].native_meta["revision"] == "Rev. 4"


def test_lettered_companions_are_not_the_standard():
    """SP 800-53A (assessment), 800-53B (baselines) and 800-171A are their own
    publications. Matching on the leading number alone would file all of them under
    the standard they accompany."""
    titles = [i.title for i in _items("sp800")]
    assert not [t for t in titles if "800-53A" in t or "800-53B" in t or "800-171A" in t]
