from __future__ import annotations

from datetime import date

from oblag import versionsuggest
from oblag.adapters.base import NormalizedDate, NormalizedItem, RawDocument
from oblag.adapters.pci_ssc import PciSscAdapter
from oblag.catalog import seed_obligations
from oblag.core.reducer import reduce_item
from oblag.db.models import Confidence, DateType, ItemState, Obligation, VersionDecision


def _publish(db, subject_slug: str, title: str, version_meta: str):
    reduce_item(
        db,
        NormalizedItem(
            source_system="pci_ssc",
            external_key=("pci_pub", f"{subject_slug}-{version_meta}"),
            jurisdiction="Global",
            title=title,
            native_status="publication",
            track="final",
            obligation_slug=subject_slug,
            dates=[
                NormalizedDate(DateType.effective, date(2026, 5, 18), Confidence.published_firm)
            ],
            native_meta={"published_version": version_meta},
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
    assert pub.track == "final"


def test_suggestion_detected_accepted_and_persisted(db):
    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-pts-hsm").update({Obligation.current_version: "4.0"})
    db.commit()
    _publish(db, "pci-pts-hsm", "PCI SSC Publishes PCI PTS HSM v5.0", "5.0")

    # the publication item lands as effective (statemap for native_status=publication)
    pub = db.query(Obligation).filter_by(slug="pci-pts-hsm").one().items[0]
    assert pub.state == ItemState.effective

    sugg = versionsuggest.pending_suggestions(db)
    assert [(s["slug"], s["in_force"], s["version"]) for s in sugg] == [
        ("pci-pts-hsm", "4.0", "5.0")
    ]

    versionsuggest.accept(db, sugg[0]["obligation_id"], "5.0", sugg[0]["item_id"], "op@example.com")
    ob = db.query(Obligation).filter_by(slug="pci-pts-hsm").one()
    assert ob.confirmed_version == "5.0"
    assert ob.effective_version == "5.0"  # newer of baseline 4.0 and confirmed 5.0
    # once accepted it no longer appears, and the decision is recorded
    assert versionsuggest.pending_suggestions(db) == []
    assert db.query(VersionDecision).filter_by(decision="accepted").count() == 1


def test_dismiss_hides_suggestion_without_changing_version(db):
    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-p2pe").update({Obligation.current_version: "3.1"})
    db.commit()
    _publish(db, "pci-p2pe", "PCI SSC Releases Version 3.2 of the PCI P2PE Standard", "3.2")

    sugg = versionsuggest.pending_suggestions(db)
    assert len(sugg) == 1 and sugg[0]["version"] == "3.2"
    versionsuggest.dismiss(db, sugg[0]["obligation_id"], "3.2", sugg[0]["item_id"], "op")

    assert versionsuggest.pending_suggestions(db) == []  # gone
    ob = db.query(Obligation).filter_by(slug="pci-p2pe").one()
    assert ob.confirmed_version is None and ob.effective_version == "3.1"  # unchanged


def test_no_suggestion_when_publication_not_newer(db):
    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-dss").update({Obligation.current_version: "4.0.1"})
    db.commit()
    _publish(db, "pci-dss", "PCI SSC Publishes PCI DSS v4.0.1", "4.0.1")  # equals in force
    assert versionsuggest.pending_suggestions(db) == []


def test_admin_versions_page_and_accept_flow(client, db):
    """The admin review page lists a detected bump and Accept applies it end-to-end."""
    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-pts-hsm").update({Obligation.current_version: "4.0"})
    db.commit()
    _publish(db, "pci-pts-hsm", "PCI SSC Publishes PCI PTS HSM v5.0", "5.0")

    page = client.get("/admin/versions").text
    assert "pci-pts-hsm" in page and "5.0" in page

    ob_id = db.query(Obligation).filter_by(slug="pci-pts-hsm").one().id
    r = client.post(
        "/admin/versions/accept",
        data={"obligation_id": ob_id, "version": "5.0", "item_id": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    assert db.query(Obligation).filter_by(slug="pci-pts-hsm").one().effective_version == "5.0"
    # the resolved suggestion is gone from the page
    assert "pci-pts-hsm" not in client.get("/admin/versions").text


def test_confirmed_version_survives_catalog_sync(engine, db, monkeypatch):
    """Accepting a bump writes confirmed_version, which the catalog sync must NOT clobber
    even though the shipped baseline is older."""
    from sqlalchemy.orm import sessionmaker

    import oblag.db.session as dbsession

    seed_obligations(db)
    ob = db.query(Obligation).filter_by(slug="pci-pts-hsm").one()
    ob.confirmed_version = "9.9"  # simulate an accepted advance beyond the catalog baseline
    db.commit()

    monkeypatch.setattr(dbsession, "_engine", engine)
    monkeypatch.setattr(
        dbsession, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    from oblag.web.app import create_app

    create_app()
    db.expire_all()
    assert db.query(Obligation).filter_by(slug="pci-pts-hsm").one().confirmed_version == "9.9"
