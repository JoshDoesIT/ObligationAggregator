from __future__ import annotations


def test_watchlist_crud_and_rss(client, seeded):
    # create an rss watchlist — server mints an unguessable token
    r = client.post(
        "/api/v1/watchlists",
        json={
            "name": "US federal changes",
            "channel": "rss",
            "filters": {"source_systems": ["federal_register"]},
        },
    )
    assert r.status_code == 201
    wl = r.json()
    assert wl["feed_url"].endswith(".xml")
    token = wl["target"]

    # feed serves matching events as RSS 2.0
    r = client.get(f"/rss/{token}.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/rss+xml")
    assert "item_created" in r.text and "CIRCIA" in r.text
    assert client.get("/rss/wrong-token.xml").status_code == 404

    # email/webhook require a target
    r = client.post("/api/v1/watchlists", json={"name": "x", "channel": "email"})
    assert r.status_code == 422

    # deleting takes it off the list and retires the feed for good
    r = client.delete(f"/api/v1/watchlists/{wl['id']}")
    assert r.status_code == 204
    assert client.get(f"/rss/{token}.xml").status_code == 410
    assert client.get("/api/v1/watchlists").json()["watchlists"] == []


def _rss_watchlist(client, name="w"):
    wl = client.post("/api/v1/watchlists", json={"name": name, "channel": "rss"}).json()
    return wl["id"], wl["target"]


def test_a_paused_feed_stays_a_feed(client, seeded):
    """It used to 404, which every reader reports as "unknown feed" — indistinguishable
    from a broken link, and there was no way back short of re-subscribing. Pausing is a
    setting the owner chose, so the channel stays valid and simply carries no entries."""
    wid, token = _rss_watchlist(client)
    assert "CIRCIA" in client.get(f"/rss/{token}.xml").text

    assert client.post(f"/api/v1/watchlists/{wid}/pause").json()["active"] is False
    paused = client.get(f"/rss/{token}.xml")
    assert paused.status_code == 200
    assert paused.headers["content-type"].startswith("application/rss+xml")
    assert "<item>" not in paused.text
    assert "paused" in paused.text.lower()

    # and resuming needs nothing from the subscriber: same token, entries return
    assert client.post(f"/api/v1/watchlists/{wid}/resume").json()["active"] is True
    assert "CIRCIA" in client.get(f"/rss/{token}.xml").text


def test_pause_is_not_delete(client, seeded):
    """The two shared one flag, so the only button on the page called DELETE."""
    wid, _token = _rss_watchlist(client)
    client.post(f"/api/v1/watchlists/{wid}/pause")
    listed = client.get("/api/v1/watchlists").json()["watchlists"]
    assert [w["id"] for w in listed] == [wid]  # paused rows are still yours
    assert listed[0]["active"] is False

    client.delete(f"/api/v1/watchlists/{wid}")
    assert client.get("/api/v1/watchlists").json()["watchlists"] == []
    # and a deleted one cannot be brought back by asking nicely
    assert client.post(f"/api/v1/watchlists/{wid}/resume").status_code == 404
    assert client.post(f"/api/v1/watchlists/{wid}/pause").status_code == 404


def test_a_paused_push_watchlist_stops_delivering(client, seeded, db, monkeypatch):
    """Pausing has to mean pausing for email and webhooks too, not just the feed."""
    from datetime import UTC, datetime

    from oblag import notify
    from oblag.db.models import Event, EventType, PipelineItem

    sent: list[str] = []
    monkeypatch.setattr(notify, "_deliver_webhook", lambda wl, batch: sent.append(wl.name))

    wl = client.post(
        "/api/v1/watchlists",
        json={"name": "ops", "channel": "webhook", "target": "https://example.com/hook"},
    ).json()

    def new_event() -> None:
        # dispatch only considers events raised after the watchlist was created
        db.add(
            Event(
                pipeline_item_id=db.query(PipelineItem).first().id,
                type=EventType.item_created,
                occurred_at=datetime.now(UTC),
                payload={},
            )
        )
        db.commit()

    new_event()
    assert notify.dispatch_pending(db) == 1 and sent == ["ops"]

    client.post(f"/api/v1/watchlists/{wl['id']}/pause")
    db.expire_all()
    new_event()
    assert notify.dispatch_pending(db) == 0
    assert sent == ["ops"]  # nothing more went out


def test_a_deleted_watchlist_keeps_its_delivery_history(client, seeded, db):
    from oblag.db.models import Watchlist

    wid, _ = _rss_watchlist(client)
    client.delete(f"/api/v1/watchlists/{wid}")
    db.expire_all()
    # the row survives on purpose: notification_log points at it, and the record of
    # what was already sent must not disappear when someone tidies up their list
    row = db.get(Watchlist, wid)
    assert row is not None and row.deleted_at is not None


def test_html_page_offers_pause_resume_and_delete(client, seeded):
    wid, _ = _rss_watchlist(client, name="Desk copy")
    page = client.get("/watchlists").text
    assert f"/watchlists/{wid}/pause" in page
    assert f"/watchlists/{wid}/delete" in page

    client.post(f"/watchlists/{wid}/pause", data={"csrf_token": ""}, follow_redirects=False)
    paused_page = client.get("/watchlists").text
    assert f"/watchlists/{wid}/resume" in paused_page

    client.post(f"/watchlists/{wid}/resume", data={"csrf_token": ""}, follow_redirects=False)
    assert f"/watchlists/{wid}/pause" in client.get("/watchlists").text

    client.post(f"/watchlists/{wid}/delete", data={"csrf_token": ""}, follow_redirects=False)
    assert "Desk copy" not in client.get("/watchlists").text
    assert client.post(f"/watchlists/{wid}/bogus", data={"csrf_token": ""}).status_code == 404


def test_subscribing_again_does_not_duplicate_a_paused_watchlist(client, seeded, db):
    from oblag.db.models import PipelineItem, Watchlist

    item = db.query(PipelineItem).first()
    client.post(f"/items/{item.id}/watch", data={"csrf_token": ""}, follow_redirects=False)
    (wl,) = db.query(Watchlist).filter(Watchlist.deleted_at.is_(None)).all()
    client.post(f"/api/v1/watchlists/{wl.id}/pause")
    client.post(f"/items/{item.id}/watch", data={"csrf_token": ""}, follow_redirects=False)
    assert db.query(Watchlist).filter(Watchlist.deleted_at.is_(None)).count() == 1


def test_watchlists_html_page(client, seeded):
    assert client.get("/watchlists").status_code == 200
