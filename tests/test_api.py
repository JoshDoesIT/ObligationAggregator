from __future__ import annotations


def test_items_list_and_filters(client, seeded):
    r = client.get("/api/v1/items")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["state"] == "comment_open"

    assert client.get("/api/v1/items?state=comment_open").json()["total"] == 1
    assert client.get("/api/v1/items?state=effective").json()["total"] == 0
    assert client.get("/api/v1/items?q=circia").json()["total"] == 1
    assert client.get("/api/v1/items?state=bogus").status_code == 422


def test_item_detail_has_provenance_and_history(client, seeded):
    item_id = client.get("/api/v1/items").json()["items"][0]["id"]
    r = client.get(f"/api/v1/items/{item_id}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["date_history"]
    assert "snapshot" in detail["date_history"][0]  # provenance always exposed
    assert detail["events"][0]["type"] == "item_created"
    assert client.get("/api/v1/items/99999").status_code == 404


def test_deadlines_countdown(client, seeded):
    r = client.get("/api/v1/deadlines?within_days=60")
    assert r.status_code == 200
    deadlines = r.json()["deadlines"]
    assert len(deadlines) == 1
    assert deadlines[0]["date_type"] == "comment_close"
    assert 0 <= deadlines[0]["days_until"] <= 31


def test_join_key_lookup(client, seeded):
    r = client.get("/api/v1/items/by-key/rin/1670-AA04")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1


def test_obligation_catalog_display_policies(client, seeded):
    obs = {o["slug"]: o for o in client.get("/api/v1/obligations").json()["obligations"]}
    assert obs["iso-27001"]["display_policy"] == "ids_only"
    assert obs["iso-27001"]["copyright_status"] == "copyrighted"
    assert obs["nist-800-53"]["display_policy"] == "full_text"


def test_html_pages_render(client, seeded):
    item_id = client.get("/api/v1/items").json()["items"][0]["id"]
    for path in ("/", f"/items/{item_id}", "/events", "/deadlines", "/health"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "ObligationAggregator" in r.text
    assert "CIRCIA" in client.get("/").text


def _rfc_item(db, key: str, title: str, slug: str):
    from datetime import date, timedelta

    from oblag.adapters.base import NormalizedDate, NormalizedItem
    from oblag.core.reducer import reduce_item
    from oblag.db.models import Confidence, DateType, PipelineItem

    future = date.today() + timedelta(days=20)
    reduce_item(
        db,
        NormalizedItem(
            source_system="pci_ssc",
            external_key=("pci_doc", key),
            jurisdiction="Global",
            title=title,
            native_status="rfc",
            track="proposed",
            obligation_slug=slug,
            dates=[NormalizedDate(DateType.comment_close, future, Confidence.published_firm)],
        ),
    )
    db.commit()
    return db.query(PipelineItem).filter_by(title=title).one()


def test_boot_syncs_catalog_fields_into_existing_db(engine, db, monkeypatch):
    """A database seeded before current_version existed must pick the values up on
    boot — the old behavior (seed only when EMPTY) left live deployments stale."""
    from sqlalchemy.orm import sessionmaker

    import oblag.db.session as dbsession
    from oblag.catalog import seed_obligations
    from oblag.db.models import Obligation

    seed_obligations(db)
    db.query(Obligation).update({Obligation.current_version: None}, synchronize_session=False)
    db.commit()

    monkeypatch.setattr(dbsession, "_engine", engine)
    monkeypatch.setattr(
        dbsession, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    from oblag.web.app import create_app

    create_app()
    db.expire_all()
    assert db.query(Obligation).filter_by(slug="pci-dss").one().current_version == "4.0.1"
    assert db.query(Obligation).filter_by(slug="pci-kmo").one().current_version is None


def test_version_parts_normalization():
    from oblag.web.html import _version_parts

    assert _version_parts("PCI PTS HSM v5.0") == ("5",)
    assert _version_parts("4.0") == _version_parts("v4")  # bare catalog value vs title token
    assert _version_parts("PCI DSS v4.0.1") == _version_parts("4.0.1")
    assert _version_parts("SP 800-53 Rev. 5.2.0") == ("5", "2")
    assert _version_parts("Rev. 5") == ("5",)
    assert _version_parts("ISO/IEC 27001 revision under development") is None
    assert _version_parts(None) is None


def test_rfc_flavors_render_the_truthful_lifecycle(client, seeded, db):
    """PCI runs RFCs in three flavors; each must render what is actually true.

    - Feedback on the in-force version (DSS v4.0.1): revision lifecycle,
      'solicits feedback on the current version'.
    - RFC on a draft of the NEXT version (PTS HSM v5.0 while v4.0 is in force):
      revision lifecycle, 'draft of the next version', current version in force.
    - RFC on a first-version draft (KMO v1.0, nothing published): the ordinary
      proposed→effective lifecycle — a v1.0 really is heading toward first
      effectiveness, and no 'in force' claim may appear."""
    from oblag.db.models import PipelineItem

    cur = _rfc_item(db, "pci-dss-401", "PCI SSC RFC: PCI DSS v4.0.1", "pci-dss")
    html = client.get(f"/items/{cur.id}").text
    assert "solicits feedback on the current version" in html
    assert "remains in force" in html and "Revision published" in html
    assert "Final · pending effective" not in html

    draft = _rfc_item(db, "pts-hsm-5", "PCI SSC RFC: PCI PTS HSM v5.0", "pci-pts-hsm")
    html = client.get(f"/items/{draft.id}").text
    assert "draft of the next" in html
    assert "remains in force" in html and "(4.0)" in html

    first = _rfc_item(db, "kmo-1", "PCI SSC RFC: PCI KMO v1.0 Standard", "pci-kmo")
    html = client.get(f"/items/{first.id}").text
    assert "remains in force" not in html
    assert "Final · pending effective" in html  # ordinary mainline stepper

    # a genuine rulemaking still uses the ordinary lifecycle (control)
    circia = db.query(PipelineItem).filter_by(source_system="federal_register").first()
    control = client.get(f"/items/{circia.id}").text
    assert "remains in force" not in control


def test_deadlines_ics_export(client, seeded):
    r = client.get("/deadlines.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert "BEGIN:VCALENDAR" in body and "END:VCALENDAR" in body
    assert "BEGIN:VEVENT" in body
    assert "Comments close: CIRCIA Reporting Requirements" in body
    assert f"DTSTART;VALUE=DATE:{seeded.strftime('%Y%m%d')}" in body


def test_quick_watch_creates_obligation_watchlist(client, seeded, db):
    from oblag.db.models import PipelineItem, Watchlist

    item = db.query(PipelineItem).first()
    r = client.post(f"/items/{item.id}/watch", follow_redirects=False)
    assert r.status_code == 303
    wl = db.query(Watchlist).filter(Watchlist.name.like("Watch:%")).one()
    assert wl.channel == "rss"
    f = wl.filters
    assert f.get("obligation_slugs") == ["circia"] or f.get("source_systems")
    # idempotent: watching again doesn't duplicate
    client.post(f"/items/{item.id}/watch", follow_redirects=False)
    assert db.query(Watchlist).filter(Watchlist.name.like("Watch:%")).count() == 1


def test_watchlist_form_accepts_obligations(client, seeded, db):
    from oblag.db.models import Watchlist

    r = client.post(
        "/watchlists",
        data={
            "name": "Org: payments + health",
            "channel": "rss",
            "obligation_slugs": ["pci-dss", "hipaa"],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    wl = db.query(Watchlist).filter_by(name="Org: payments + health").one()
    assert wl.filters["obligation_slugs"] == ["pci-dss", "hipaa"]
