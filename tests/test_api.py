from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from oblag.adapters.base import NormalizedDate, NormalizedItem
from oblag.catalog import seed_obligations
from oblag.core.reducer import reduce_item
from oblag.db.models import Confidence, DateType


@pytest.fixture()
def client(engine, db, monkeypatch):
    # Point the app's session factory at the test engine
    from sqlalchemy.orm import sessionmaker

    import oblag.db.session as dbsession

    monkeypatch.setattr(dbsession, "_engine", engine)
    monkeypatch.setattr(
        dbsession, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    from oblag.web.app import create_app

    return TestClient(create_app())


@pytest.fixture()
def seeded(db):
    seed_obligations(db)
    future = date.today() + timedelta(days=30)
    reduce_item(
        db,
        NormalizedItem(
            source_system="federal_register",
            external_key=("fr_doc_number", "2024-06526"),
            jurisdiction="US-Federal",
            title="CIRCIA Reporting Requirements",
            native_status="PRORULE",
            track="proposed",
            join_keys=[("rin", "1670-AA04")],
            dates=[NormalizedDate(DateType.comment_close, future, Confidence.published_firm)],
        ),
    )
    db.commit()
    return future


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
