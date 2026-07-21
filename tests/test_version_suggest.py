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


def test_admin_versions_page_shows_audit_log(client, db):
    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-pts-hsm").update({Obligation.current_version: "4.0"})
    db.commit()
    _publish(db, "pci-pts-hsm", "PCI SSC Publishes PCI PTS HSM v5.0", "5.0")
    versionsuggest.auto_apply(db)

    page = client.get("/admin/versions").text
    assert "pci-pts-hsm" in page and "5.0" in page and "applied" in page
