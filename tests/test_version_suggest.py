from __future__ import annotations

from datetime import date

from oblag import versionsuggest
from oblag.adapters.base import NormalizedDate, NormalizedItem, RawDocument
from oblag.adapters.pci_ssc import PciSscAdapter
from oblag.catalog import seed_obligations
from oblag.core.reducer import reduce_item
from oblag.db.models import Confidence, DateType, ItemState, Obligation, VersionDecision


def _publish(db, slug: str, title: str, version: str):
    reduce_item(
        db,
        NormalizedItem(
            source_system="pci_ssc",
            external_key=("pci_pub", f"{slug}-{version}"),
            jurisdiction="Global",
            title=title,
            native_status="publication",
            track="final",
            obligation_slug=slug,
            dates=[
                NormalizedDate(DateType.effective, date(2026, 5, 18), Confidence.published_firm)
            ],
            native_meta={"published_version": version},
        ),
    )
    db.commit()


def _publish_iso(db, slug: str, year: str):
    reduce_item(
        db,
        NormalizedItem(
            source_system="iso_catalog",
            external_key=("iso_project", slug),
            jurisdiction="Global",
            title=f"ISO {slug} edition",
            native_status="60.60",  # published stage → effective
            track="default",
            obligation_slug=slug,
            native_meta={"publication_date": year},
        ),
    )
    db.commit()


def test_pci_adapter_emits_publication_items():
    """A 'Just Published … vN' post becomes an effective publication item; a versionless
    'Publishes new guidance' post does not (no version → no signal)."""
    rss = b"""<?xml version="1.0"?><rss><channel>
    <item><title>PCI SSC Publishes PCI PTS HSM v5.0</title>
      <link>https://blog.pcisecuritystandards.org/hsm5</link>
      <pubDate>Mon, 18 May 2026 00:00:00 GMT</pubDate></item>
    <item><title>PCI SSC Publishes New Guidance on Compensating Controls</title>
      <link>https://blog.pcisecuritystandards.org/guidance</link>
      <pubDate>Mon, 04 May 2026 00:00:00 GMT</pubDate></item>
    </channel></rss>"""
    items = list(PciSscAdapter().normalize(RawDocument(url="https://t", content=rss)))
    assert len(items) == 1
    pub = items[0]
    assert pub.native_status == "publication"
    assert pub.obligation_slug == "pci-pts-hsm"
    assert pub.native_meta["published_version"] == "5.0"


def test_auto_apply_advances_plausible_version(db):
    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-pts-hsm").update({Obligation.current_version: "4.0"})
    db.commit()
    _publish(db, "pci-pts-hsm", "PCI SSC Publishes PCI PTS HSM v5.0", "5.0")

    pub = db.query(Obligation).filter_by(slug="pci-pts-hsm").one().items[0]
    assert pub.state == ItemState.effective

    actions = versionsuggest.auto_apply(db)
    assert actions == [{"slug": "pci-pts-hsm", "version": "5.0", "applied": True}]
    ob = db.query(Obligation).filter_by(slug="pci-pts-hsm").one()
    assert ob.confirmed_version == "5.0" and ob.effective_version == "5.0"
    assert db.query(VersionDecision).filter_by(decision="auto").count() == 1


def test_auto_apply_is_idempotent(db):
    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-p2pe").update({Obligation.current_version: "3.1"})
    db.commit()
    _publish(db, "pci-p2pe", "PCI SSC Releases Version 3.2 of the PCI P2PE Standard", "3.2")

    assert versionsuggest.auto_apply(db)[0]["applied"] is True
    # a second pass does nothing new — the version is already ruled on
    assert versionsuggest.auto_apply(db) == []
    assert db.query(VersionDecision).count() == 1
    assert db.query(Obligation).filter_by(slug="pci-p2pe").one().effective_version == "3.2"


def test_auto_apply_flags_implausible_jump_without_changing_version(db):
    """A wild major jump (the fingerprint of a mis-parse) is flagged, never applied."""
    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-dss").update({Obligation.current_version: "4.0.1"})
    db.commit()
    _publish(db, "pci-dss", "PCI SSC Publishes PCI DSS v12.0", "12.0")

    actions = versionsuggest.auto_apply(db)
    assert actions == [{"slug": "pci-dss", "version": "12.0", "applied": False}]
    ob = db.query(Obligation).filter_by(slug="pci-dss").one()
    assert ob.confirmed_version is None and ob.effective_version == "4.0.1"  # unchanged
    assert db.query(VersionDecision).filter_by(decision="flagged").count() == 1


def test_auto_apply_iso_edition_year(db):
    seed_obligations(db)
    db.query(Obligation).filter_by(slug="iso-27018").update({Obligation.current_version: "2019"})
    db.commit()
    _publish_iso(db, "iso-27018", "2025")

    assert versionsuggest.auto_apply(db)[0] == {
        "slug": "iso-27018",
        "version": "2025",
        "applied": True,
    }
    assert db.query(Obligation).filter_by(slug="iso-27018").one().effective_version == "2025"


def test_catalog_edit_overrides_auto_value(engine, db, monkeypatch):
    """A catalog current_version edit is the always-wins override: on the next boot it
    clears any auto-detected value for that standard so the corrected baseline takes."""
    from sqlalchemy.orm import sessionmaker

    import oblag.db.session as dbsession

    seed_obligations(db)
    # simulate a bad auto-detection, with the DB baseline lagging the shipped catalog
    # (pci-pts-hsm ships as 5.0) — i.e. the catalog was corrected to 5.0
    ob = db.query(Obligation).filter_by(slug="pci-pts-hsm").one()
    ob.current_version, ob.confirmed_version = "4.0", "9.9"
    db.commit()

    monkeypatch.setattr(dbsession, "_engine", engine)
    monkeypatch.setattr(
        dbsession, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    from oblag.web.app import create_app

    create_app()
    db.expire_all()
    ob = db.query(Obligation).filter_by(slug="pci-pts-hsm").one()
    assert ob.confirmed_version is None  # auto value cleared by the catalog edit
    assert ob.current_version == "5.0" and ob.effective_version == "5.0"


def test_confirmed_version_untouched_when_baseline_matches(engine, db, monkeypatch):
    """A boot with no catalog version change preserves an auto-applied value."""
    from sqlalchemy.orm import sessionmaker

    import oblag.db.session as dbsession

    seed_obligations(db)  # DB now matches the catalog exactly
    db.query(Obligation).filter_by(slug="pci-mpoc").update({Obligation.confirmed_version: "1.2"})
    db.commit()

    monkeypatch.setattr(dbsession, "_engine", engine)
    monkeypatch.setattr(
        dbsession, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    from oblag.web.app import create_app

    create_app()
    db.expire_all()
    assert db.query(Obligation).filter_by(slug="pci-mpoc").one().confirmed_version == "1.2"


def test_flagged_newest_does_not_block_plausible_advance(db):
    """A mis-parse (v12.0, flagged) must not shadow the real advance (v5.0) behind it."""
    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-pts-hsm").update({Obligation.current_version: "4.0"})
    db.commit()
    _publish(db, "pci-pts-hsm", "PCI SSC Publishes PCI PTS HSM v12.0", "12.0")
    _publish(db, "pci-pts-hsm", "PCI SSC Publishes PCI PTS HSM v5.0", "5.0")

    actions = {(a["version"], a["applied"]) for a in versionsuggest.auto_apply(db)}
    assert actions == {("12.0", False), ("5.0", True)}
    assert db.query(Obligation).filter_by(slug="pci-pts-hsm").one().effective_version == "5.0"


def test_supporting_document_posts_are_not_publications():
    """SAQ/AOC/translation posts name a standard + version without BEING one."""
    rss = b"""<?xml version="1.0"?><rss><channel>
    <item><title>Just Published: PCI DSS v4.0.1 SAQ A</title>
      <link>https://x/saq</link><pubDate>Mon, 01 Jul 2024 00:00:00 GMT</pubDate></item>
    <item><title>PCI DSS v4.0 Summary of Changes now available</title>
      <link>https://x/soc</link><pubDate>Mon, 01 Jul 2024 00:00:00 GMT</pubDate></item>
    <item><title>PCI DSS v4.0 Now Available in Spanish</title>
      <link>https://x/es</link><pubDate>Mon, 01 Jul 2024 00:00:00 GMT</pubDate></item>
    </channel></rss>"""
    assert list(PciSscAdapter().normalize(RawDocument(url="https://t", content=rss))) == []


def _rfc(db, slug: str, title: str, opened: date, closed: date):
    reduce_item(
        db,
        NormalizedItem(
            source_system="pci_ssc",
            external_key=("pci_doc", title.lower().replace(" ", "-")),
            jurisdiction="Global",
            title=title,
            native_status="rfc",
            track="proposed",
            obligation_slug=slug,
            dates=[
                NormalizedDate(DateType.comment_open, opened, Confidence.published_firm),
                NormalizedDate(DateType.comment_close, closed, Confidence.published_firm),
            ],
        ),
    )
    db.commit()


def test_publication_resolves_the_consultation_that_drafted_it(db):
    """Once v5.0 is published, the 'PTS HSM v5.0' RFC has concluded → superseded,
    linked to the publication item. But an RFC ON the current version (subject equals
    a version published BEFORE the RFC opened) is never claimed as resolved."""
    from oblag.db.models import PipelineItem

    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-pts-hsm").update({Obligation.current_version: "4.0"})
    db.query(Obligation).filter_by(slug="pci-dss").update({Obligation.current_version: "4.0.1"})
    db.commit()

    # draft RFC (Oct–Dec 2025), then the version publishes May 2026 → resolved
    _rfc(db, "pci-pts-hsm", "PCI SSC RFC: PCI PTS HSM v5.0", date(2025, 10, 30), date(2025, 12, 15))
    _publish(db, "pci-pts-hsm", "PCI SSC Publishes PCI PTS HSM v5.0", "5.0")  # eff 2026-05-18

    # feedback-on-current RFC (June 2026) on v4.0.1, which published back in 2024
    _rfc(db, "pci-dss", "PCI SSC RFC: PCI DSS v4.0.1", date(2026, 6, 3), date(2026, 7, 20))
    reduce_item(
        db,
        NormalizedItem(
            source_system="pci_ssc",
            external_key=("pci_pub", "pci-dss-4.0.1"),
            jurisdiction="Global",
            title="Just Published PCI DSS v4.0.1",
            native_status="publication",
            track="final",
            obligation_slug="pci-dss",
            dates=[
                NormalizedDate(DateType.effective, date(2024, 6, 11), Confidence.published_firm)
            ],
            native_meta={"published_version": "4.0.1"},
        ),
    )
    db.commit()

    versionsuggest.resolve_concluded_consultations(db)
    db.commit()

    hsm_rfc = db.query(PipelineItem).filter_by(title="PCI SSC RFC: PCI PTS HSM v5.0").one()
    assert hsm_rfc.state == ItemState.superseded
    assert hsm_rfc.resolved_change_id is not None
    dss_rfc = db.query(PipelineItem).filter_by(title="PCI SSC RFC: PCI DSS v4.0.1").one()
    assert dss_rfc.state != ItemState.superseded  # older publication is not its outcome
    assert dss_rfc.resolved_change_id is None


def test_superseded_item_reingest_is_noop_not_anomaly(db):
    """The RFC post stays in the blog feed for months after resolution; re-reducing it
    must not spam illegal-transition anomalies."""
    from oblag.db.models import Event, EventType, PipelineItem

    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-pts-hsm").update({Obligation.current_version: "4.0"})
    db.commit()
    _rfc(db, "pci-pts-hsm", "PCI SSC RFC: PCI PTS HSM v5.0", date(2025, 10, 30), date(2025, 12, 15))
    _publish(db, "pci-pts-hsm", "PCI SSC Publishes PCI PTS HSM v5.0", "5.0")
    versionsuggest.resolve_concluded_consultations(db)
    db.commit()

    before = db.query(Event).filter_by(type=EventType.anomaly).count()
    # same RFC arrives again from the still-live feed
    _rfc(db, "pci-pts-hsm", "PCI SSC RFC: PCI PTS HSM v5.0", date(2025, 10, 30), date(2025, 12, 15))
    assert db.query(Event).filter_by(type=EventType.anomaly).count() == before
    item = db.query(PipelineItem).filter_by(title="PCI SSC RFC: PCI PTS HSM v5.0").one()
    assert item.state == ItemState.superseded  # resolution sticks


def test_release_items_get_factual_banners_not_consultation_wording(client, db):
    """Effective release items must state where the version stands — never 'draft of
    the next version' (live bug: HITRUST CSF v11.4.0 with 11.8 in force)."""
    seed_obligations(db)  # hitrust-csf ships with current_version 11.8
    reduce_item(
        db,
        NormalizedItem(
            source_system="hitrust",
            external_key=("hitrust_url", "csf-v11-4-launch"),
            jurisdiction="Global",
            title="HITRUST CSF v11.4.0",
            native_status="release",
            track="default",
            obligation_slug="hitrust-csf",
        ),
    )
    db.commit()
    from oblag.db.models import PipelineItem

    old = db.query(PipelineItem).filter_by(title="HITRUST CSF v11.4.0").one()
    assert old.state == ItemState.effective
    html = client.get(f"/items/{old.id}").text
    assert "superseded" in html and "11.8" in html
    assert "draft of the next" not in html and "solicits feedback" not in html
    assert "Comment open" not in html  # no fabricated comment lifecycle

    # a release OF the current version reads as current, not superseded
    reduce_item(
        db,
        NormalizedItem(
            source_system="hitrust",
            external_key=("hitrust_url", "csf-v11-8-launch"),
            jurisdiction="Global",
            title="HITRUST CSF v11.8.0",
            native_status="release",
            track="default",
            obligation_slug="hitrust-csf",
        ),
    )
    db.commit()
    cur = db.query(PipelineItem).filter_by(title="HITRUST CSF v11.8.0").one()
    html = client.get(f"/items/{cur.id}").text
    assert "current version" in html and "in force" in html
    assert "superseded" not in html


def test_advisory_items_are_informational_without_lifecycle_claims(client, db):
    seed_obligations(db)
    reduce_item(
        db,
        NormalizedItem(
            source_system="hitrust",
            external_key=("hitrust_url", "haa-2026-002"),
            jurisdiction="Global",
            title="HITRUST advisory HAA-2026-002",
            native_status="advisory",
            track="default",
            obligation_slug="hitrust-csf",
        ),
    )
    db.commit()
    from oblag.db.models import PipelineItem

    adv = db.query(PipelineItem).filter_by(title="HITRUST advisory HAA-2026-002").one()
    html = client.get(f"/items/{adv.id}").text
    assert "advisory" in html and "informational" in html
    assert "Comment open" not in html and "remains in force" not in html


def test_closed_current_version_consultation_uses_past_tense(client, db):
    """A CLOSED consultation whose subject equals the in-force version must not claim
    to be soliciting feedback — true for both a closed feedback-on-current RFC and a
    historical draft RFC whose version has since published (item 275 live case)."""
    from oblag.db.models import PipelineItem

    seed_obligations(db)  # pts-hsm ships with current_version 5.0
    _rfc(db, "pci-pts-hsm", "PCI SSC RFC: PCI PTS HSM v5.0", date(2025, 10, 30), date(2025, 12, 15))
    rfc = db.query(PipelineItem).filter_by(title="PCI SSC RFC: PCI PTS HSM v5.0").one()
    assert rfc.state == ItemState.comment_closed
    html = client.get(f"/items/{rfc.id}").text
    assert "currently in force" in html and "comment window has closed" in html
    assert "solicits feedback" not in html


def test_admin_versions_page_shows_audit_log(client, db):
    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-pts-hsm").update({Obligation.current_version: "4.0"})
    db.commit()
    _publish(db, "pci-pts-hsm", "PCI SSC Publishes PCI PTS HSM v5.0", "5.0")
    versionsuggest.auto_apply(db)

    page = client.get("/admin/versions").text
    assert "pci-pts-hsm" in page and "5.0" in page and "applied" in page
