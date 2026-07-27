"""Relevance: what gets in, what gets cleared out, and what the feed shows about it."""

from __future__ import annotations

from datetime import date
from hashlib import sha256

from oblag.adapters.base import NormalizedItem
from oblag.core.reducer import reduce_item
from oblag.db.models import PipelineItem
from oblag.scope import in_scope

# Verbatim from production, with the term that admitted each one. Every single one had
# NO security or privacy word in its title — they rode in on the abstract.
LIVE_NOISE = [
    ("Revising HUD's Noise Abatement and Control Regulations", "aerial surveillance imagery"),
    ("American Fisheries Act Program Update", "collects personally identifiable information"),
    (
        "Establishing United States Citizenship for MARAD Program Participation",
        "personally identifiable information is protected",
    ),
    ("Tanker Security Program", "vessel security requirements apply"),
    (
        "Counter-UAS Authority for State, Local, Tribal, and Territorial Law Enforcement",
        "consistent with privacy protections",
    ),
    (
        "Request for Comment on the Extension of Standard Futures Contracts to 24/7 Trading",
        "market surveillance obligations",
    ),
    (
        "Fisheries of the Exclusive Economic Zone Off Alaska; Inseason Adjustment",
        "the ai (Aleutian Islands) subarea",
    ),
    (
        "Reforming the High-Cost Program for an All-IP Future",
        "artificial intelligence may assist deployment",
    ),
]


def test_a_passing_mention_in_the_abstract_no_longer_admits_a_rule():
    for title, abstract in LIVE_NOISE:
        assert not in_scope(title, abstract), title


def test_the_same_words_in_a_TITLE_still_admit_it():
    """The weak list is not deleted, only demoted. A title IS a statement of subject."""
    for title in (
        "Collection and Use of Biometrics by U.S. Citizenship and Immigration Services",
        "Privacy Act of 1974; Implementation",
        "Artificial Intelligence Risk Management Framework",
        "Critical Infrastructure Protection Requirements",
    ):
        assert in_scope(title, "some unrelated abstract"), title


def test_strong_terms_still_fire_from_body_text():
    """A rule whose abstract says "ransomware" is about ransomware, whatever it is
    called. Losing that would be the expensive kind of mistake."""
    for abstract in (
        "requires reporting of ransomware payments",
        "implements breach notification obligations",
        "amends the HIPAA Security Rule",
        "establishes cybersecurity requirements for contractors",
    ):
        assert in_scope("Miscellaneous Amendments; Technical Corrections", abstract), abstract


def test_a_single_string_gets_the_full_vocabulary():
    """CELLAR calls in_scope(title) with one argument. Treating that as body text would
    silently narrow the EU feed."""
    assert in_scope("Commission Decision on privacy of electronic communications")


def _fr(db, title: str, abstract: str, obligation: str | None = None):
    return reduce_item(
        db,
        NormalizedItem(
            source_system="federal_register",
            external_key=("fr_doc_number", f"doc-{sha256(title.encode()).hexdigest()[:12]}"),
            jurisdiction="US-Federal",
            title=title,
            abstract=abstract,
            native_status="RULE",
            track="final",
            obligation_slug=obligation,
        ),
        today=date(2026, 7, 27),
    )


def test_rescope_clears_noise_already_in_the_feed(db):
    from oblag.catalog import seed_obligations
    from oblag.maintenance import rescope_items

    seed_obligations(db)
    for title, abstract in LIVE_NOISE[:3]:
        _fr(db, title, abstract)
    keeper = _fr(db, "Cybersecurity Incident Reporting Requirements", "as described")
    db.commit()

    purged = rescope_items(db)
    db.commit()
    assert len(purged) == 3
    assert db.query(PipelineItem).one().id == keeper.item.id


def test_rescope_never_touches_an_item_linked_to_a_tracked_obligation(db):
    """The CELEX corrigenda to DORA and NIS2 have bare-id titles like "32022R2554R(09)"
    and would fail any keyword test. A link to something we track outranks wording."""
    from oblag.catalog import seed_obligations
    from oblag.maintenance import rescope_items

    seed_obligations(db)
    linked = _fr(db, "32022R2554R(09)", "corrigendum", obligation="dora")
    db.commit()
    assert rescope_items(db) == []
    assert db.get(PipelineItem, linked.item.id) is not None


def test_rescope_never_judges_a_source_that_never_consults_the_gate(db):
    from oblag.catalog import seed_obligations
    from oblag.maintenance import rescope_items

    seed_obligations(db)
    reduce_item(
        db,
        NormalizedItem(
            source_system="hitrust",
            external_key=("hitrust_release", "11.8.0"),
            jurisdiction="Global",
            title="HITRUST CSF v11.8.0",
            native_status="release",
            track="final",
        ),
        today=date(2026, 7, 27),
    )
    db.commit()
    assert rescope_items(db) == []
    assert db.query(PipelineItem).count() == 1


def test_nerc_is_retired_from_the_catalog_and_the_adapters(db):
    from oblag.adapters import available_adapters
    from oblag.catalog import CATALOG, RETIRED_OBLIGATIONS, seed_obligations
    from oblag.db.models import Obligation

    assert "nerc" not in available_adapters()
    assert "nerc-cip" not in {e["slug"] for e in CATALOG}
    assert "nerc-cip" in RETIRED_OBLIGATIONS
    seed_obligations(db)
    db.commit()
    assert db.query(Obligation).filter_by(slug="nerc-cip").one_or_none() is None


def test_retiring_an_obligation_takes_its_items_and_references_with_it(db):
    """A row left pointing at a deleted obligation is worse than no row, and an org
    still scoped to it would be following something that cannot appear."""
    from oblag.catalog import seed_obligations
    from oblag.db.models import Obligation, Org, Watchlist

    seed_obligations(db)
    db.commit()
    # re-create the retired obligation as an older deployment would have left it
    ob = Obligation(slug="nerc-cip", name="NERC CIP", issuing_body="NERC", jurisdiction="US")
    db.add(ob)
    db.flush()
    item = PipelineItem(
        source_system="nerc",
        jurisdiction="US-Federal",
        title="NERC Project 2023-03",
        native_status="proposed",
        track="proposed",
        content_fingerprint="x",
        obligation_id=ob.id,
    )
    from oblag.db.models import ItemState

    item.state = ItemState.proposed
    db.add(item)
    from oblag.auth import get_default_org

    org = get_default_org(db)
    org.scoped_obligations = ["nerc-cip", "gdpr"]
    db.add(
        Watchlist(
            org_id=org.id,
            name="w",
            channel="rss",
            filters={"obligation_slugs": ["nerc-cip", "gdpr"]},
        )
    )
    db.commit()

    seed_obligations(db)
    db.commit()
    db.expire_all()
    assert db.query(Obligation).filter_by(slug="nerc-cip").one_or_none() is None
    assert db.query(PipelineItem).filter_by(source_system="nerc").count() == 0
    assert db.query(Org).first().scoped_obligations == ["gdpr"]
    assert db.query(Watchlist).one().filters["obligation_slugs"] == ["gdpr"]


def test_purge_retired_sources_clears_items_no_adapter_can_maintain(db):
    from oblag.adapters import available_adapters
    from oblag.db.models import ItemState
    from oblag.maintenance import purge_retired_sources

    orphan = PipelineItem(
        source_system="nerc",
        jurisdiction="US-Federal",
        title="NERC Project 2022-05",
        state=ItemState.proposed,
        native_status="proposed",
        track="proposed",
        content_fingerprint="y",
    )
    db.add(orphan)
    db.commit()
    assert purge_retired_sources(db, set(available_adapters())) == [orphan.id]
    db.commit()
    assert db.query(PipelineItem).count() == 0
    # curated rows are a person's work and never belong to an adapter
    assert purge_retired_sources(db, set(available_adapters())) == []


def test_the_feed_shows_the_date_it_is_ordered_by(client, seeded):
    """The feed sorts by the source's publication date, and that date was nowhere on
    the row — the only dates shown were deadlines, which run the other way, so the
    order read as arbitrary."""
    html = client.get("/changes").text
    assert 'class="filed"' in html


def test_the_feed_is_actually_chronological(client, seeded, db):
    """The page used to re-sort the API's chronological result by (lifecycle state,
    next deadline, id). Rows then jumped between years down the page, ordered by a key
    that was never displayed. Urgency lives in the attention band and /deadlines."""
    import re as _re

    html = client.get("/changes").text
    filed = _re.findall(r'<span class="filed">([^<]+)</span>', html)
    assert filed, "every row states the date it is ordered by"
    dated = [f for f in filed if f != "undated"]
    assert dated == sorted(dated, reverse=True), f"not newest-first: {dated}"
    # undated rows sort last rather than riding our ingestion clock to the top
    if "undated" in filed:
        assert filed.index("undated") >= len(dated)


def test_removing_a_catalog_entry_counts_as_drift(db, monkeypatch):
    """_sync_catalog only compared the entries CATALOG still ships, so a REMOVED one
    was invisible and retirement never ran. nerc-cip stayed live in production through
    a deploy that had already deleted its adapter."""
    from oblag.catalog import CATALOG, RETIRED_OBLIGATIONS, seed_obligations
    from oblag.db.models import Obligation

    seed_obligations(db)
    db.add(Obligation(slug="nerc-cip", name="NERC CIP", issuing_body="NERC", jurisdiction="US"))
    db.commit()

    rows = {o.slug: o for o in db.query(Obligation).all()}
    shipped_drift = any(
        (row := rows.get(entry["slug"])) is None
        or any(getattr(row, field) != value for field, value in entry.items())
        for entry in CATALOG
    )
    assert not shipped_drift, "nothing the catalog still ships has changed"
    assert any(slug in rows for slug in RETIRED_OBLIGATIONS), "yet a retired one is live"


def test_the_aicpa_filter_is_re_applied_to_rows_already_stored(db):
    """Tightening a URL filter only stops new rows. Seven CPA professional-conduct
    drafts were live when "ethics" left the include list."""
    from oblag.db.models import ItemState
    from oblag.maintenance import rescope_sitemap_items

    urls = [
        "https://www.aicpa-cima.com/news/download/ethics-exposure-draft-tax-services",
        "https://www.aicpa-cima.com/news/download/ethics-exposure-draft-section-529-plans",
        "https://www.aicpa-cima.com/resources/download/exposure-draft-proposed-ssae-qm",
    ]
    for url in urls:
        item = PipelineItem(
            source_system="aicpa",
            jurisdiction="Global",
            title=f"AICPA exposure draft: {url.rsplit('/', 1)[-1]}",
            url=url,
            state=ItemState.proposed,
            native_status="exposure_draft",
            track="proposed",
            content_fingerprint=sha256(url.encode()).hexdigest(),
        )
        db.add(item)
    db.commit()

    purged = rescope_sitemap_items(db)
    db.commit()
    assert len(purged) == 2
    survivor = db.query(PipelineItem).one()
    assert "ssae-qm" in survivor.url
    assert rescope_sitemap_items(db) == []  # idempotent
