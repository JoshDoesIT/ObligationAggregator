from __future__ import annotations

from datetime import date

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.cellar import CELEX_OBLIGATION_MAP, CellarAdapter
from oblag.catalog import CATALOG, seed_obligations
from oblag.core.reducer import reduce_item
from oblag.db.models import CopyrightStatus, DisplayPolicy, Obligation

EXPECTED_NEW_SLUGS = {
    "glba-safeguards",
    "coppa",
    "sec-cyber-disclosure",
    "nist-800-63",
    "nist-privacy-framework",
    "fips-140-3",
    "nydfs-500",
    "eu-cra",
    "eidas2",
    "uk-gdpr",
    "pipeda",
    "lgpd",
    "iso-27701",
    "iso-27017",
    "iso-27018",
    "iso-22301",
    "cis-controls",
    "csa-ccm",
    "hitrust-csf",
    "nerc-cip",
}


def test_catalog_contains_expanded_set(db):
    n = seed_obligations(db)
    slugs = {o.slug for o in db.query(Obligation)}
    assert slugs >= EXPECTED_NEW_SLUGS
    assert n == len(CATALOG) == len(slugs)  # no duplicate slugs collapse silently


def test_seed_is_idempotent_and_updates(db):
    seed_obligations(db)
    first = db.query(Obligation).count()
    seed_obligations(db)
    assert db.query(Obligation).count() == first


def test_copyright_postures_are_conservative(db):
    seed_obligations(db)
    by_slug = {o.slug: o for o in db.query(Obligation)}
    # every copyrighted obligation must be gated below full_text
    for ob in by_slug.values():
        if ob.copyright_status is CopyrightStatus.copyrighted:
            assert ob.display_policy is not DisplayPolicy.full_text, ob.slug
    # ISO family: ids_only (litigious); HITRUST most restrictive
    for slug in ("iso-27701", "iso-27017", "iso-27018", "iso-22301"):
        assert by_slug[slug].display_policy is DisplayPolicy.ids_only
    assert by_slug["hitrust-csf"].display_policy is DisplayPolicy.events_only
    # government works stay open
    assert by_slug["nydfs-500"].copyright_status is CopyrightStatus.public_domain


def test_iso_entries_are_auto_watched_by_iso_catalog_adapter(db, engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    import oblag.db.session as dbsession
    from oblag.adapters.base import FetchContext
    from oblag.adapters.iso_catalog import IsoCatalogAdapter

    seed_obligations(db)
    db.commit()
    monkeypatch.setattr(dbsession, "_engine", engine)
    monkeypatch.setattr(
        dbsession, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    watched = dict(IsoCatalogAdapter()._watched(FetchContext(client=None)))
    for slug in (
        "iso-27001",
        "iso-27002",
        "iso-42001",
        "iso-27701",
        "iso-27017",
        "iso-27018",
        "iso-22301",
    ):
        assert slug in watched, slug
    # 22301/27017 must use the verified numeric pages (vanity URLs 404)
    assert watched["iso-22301"].endswith("75106.html")
    assert watched["iso-27017"].endswith("43757.html")


def test_cellar_items_link_to_obligations(db):
    seed_obligations(db)
    assert CELEX_OBLIGATION_MAP["32024R1689"] == "eu-ai-act"
    adapter = CellarAdapter()
    raw = RawDocument(
        url="https://sparql",
        content=load_fixture("cellar", "acts_aiact_window.json"),
        meta={"kind": "acts"},
    )
    ai = next(i for i in adapter.normalize(raw) if i.external_key == ("celex", "32024R1689"))
    assert ai.obligation_slug == "eu-ai-act"
    res = reduce_item(db, ai, today=date(2024, 7, 1))
    assert res.item.obligation is not None
    assert res.item.obligation.slug == "eu-ai-act"


def test_nist_series_map_extended():
    from oblag.adapters.nist_csrc import _OBLIGATION_MAP

    assert _OBLIGATION_MAP[("SP", "800-63")] == "nist-800-63"
    assert _OBLIGATION_MAP[("FIPS", "140-3")] == "fips-140-3"
    # every mapped slug exists in the shipped catalog
    catalog_slugs = {e["slug"] for e in CATALOG}
    assert set(_OBLIGATION_MAP.values()) <= catalog_slugs
    assert set(CELEX_OBLIGATION_MAP.values()) <= catalog_slugs
