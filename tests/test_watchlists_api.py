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

    # soft delete disables the feed
    r = client.delete(f"/api/v1/watchlists/{wl['id']}")
    assert r.status_code == 204
    assert client.get(f"/rss/{token}.xml").status_code == 404


def test_watchlists_html_page(client, seeded):
    assert client.get("/watchlists").status_code == 200
