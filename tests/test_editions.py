"""A bookmarkable edition: the obligations you picked, on any device, without an account."""

from __future__ import annotations

import re


def _edition_url(client) -> str:
    page = client.get("/obligations").text
    match = re.search(r'id="edition-url" value="([^"]+)"', page)
    assert match, "the obligations page should offer a bookmarkable link once you have picked"
    return match.group(1).replace("http://testserver", "")


def _showing(client) -> str:
    return re.search(r"Currently showing ([\w ]+)", client.get("/obligations").text).group(1)


def _pick(client, *slugs):
    return client.post(
        "/obligations/scope",
        data={"csrf_token": "", "slugs": list(slugs)},
        follow_redirects=False,
    )


def test_no_link_is_offered_before_anything_is_picked(client, seeded):
    assert 'id="edition-url"' not in client.get("/obligations").text


def test_the_link_carries_the_selection_to_a_device_that_has_never_seen_it(client, seeded, app):
    """The whole point: bookmark it, open it on a phone, read the same curated site."""
    from fastapi.testclient import TestClient

    _pick(client, "dora", "gdpr")
    url = _edition_url(client)

    phone = TestClient(app)  # a different device, no cookies
    adopt = phone.get(url, follow_redirects=False)
    assert adopt.status_code == 303 and adopt.headers["location"] == "/"
    assert "oblag_edition" in adopt.headers.get("set-cookie", "")
    assert _showing(phone) == "2"


def test_the_link_survives_refining_the_selection(client, seeded, app):
    """A bookmark that breaks the moment you change your list is worthless, so editing
    updates the edition in place rather than minting a second one."""
    from fastapi.testclient import TestClient

    _pick(client, "dora")
    url = _edition_url(client)
    phone = TestClient(app)
    phone.get(url)

    _pick(client, "dora", "gdpr", "nis2")
    assert _edition_url(client) == url  # same token
    assert _showing(phone) == "3"  # and the phone follows along with no new bookmark


def test_an_edition_beats_the_instance_wide_setting(client, seeded, app, db):
    """Org scope is one value for the whole deployment. Without an edition winning over
    it there is no way to have your own selection on a shared instance."""
    from fastapi.testclient import TestClient

    from oblag.db.models import Org

    _pick(client, "dora", "gdpr")
    url = _edition_url(client)

    phone = TestClient(app)
    phone.get(url)
    # someone else changes the shared default; the phone keeps reading its edition
    org = db.query(Org).first()
    org.scoped_obligations = ["ccpa"]
    db.commit()
    assert _showing(phone) == "2"


def test_leaving_forgets_the_edition_here_without_destroying_it(client, seeded, app):
    from fastapi.testclient import TestClient

    _pick(client, "dora", "gdpr")
    url = _edition_url(client)
    phone = TestClient(app)
    phone.get(url)
    assert _showing(phone) == "2"

    left = phone.get("/e", follow_redirects=False)
    assert left.status_code == 303
    phone.cookies.clear()
    # the link still works — leaving is a per-device thing
    phone.get(url)
    assert _showing(phone) == "2"


def test_a_dead_bookmark_lands_somewhere_useful_rather_than_on_an_error(client, seeded):
    r = client.get("/e/not-a-real-token", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/obligations")
    assert client.get("/obligations").status_code == 200


def test_the_edition_scopes_the_reading_surfaces_not_just_the_catalog(client, seeded, app):
    """The catalog always shows everything (it is where you choose); the feed, deadlines
    and front page are what the edition narrows."""
    from fastapi.testclient import TestClient

    _pick(client, "dora")
    url = _edition_url(client)
    phone = TestClient(app)
    phone.get(url)
    for path in ("/", "/changes", "/deadlines"):
        assert phone.get(path).status_code == 200
    catalog = phone.get("/obligations").text
    assert "CIRCIA" in catalog or "Obligation catalog" in catalog


def test_the_adopt_redirect_is_never_cached_by_a_cdn(client, seeded, app):
    """It sets a cookie, so an edge cache serving it to the next visitor would hand them
    somebody else's edition."""
    from fastapi.testclient import TestClient

    _pick(client, "dora")
    url = _edition_url(client)
    r = TestClient(app).get(url, follow_redirects=False)
    assert "no-store" in r.headers.get("cache-control", "")
    assert r.headers.get("Vercel-CDN-Cache-Control", "no-store") == "no-store"


def test_the_token_is_not_guessable(client, seeded, db):
    from oblag.db.models import Edition

    _pick(client, "dora")
    (row,) = db.query(Edition).all()
    assert len(row.token) >= 16
    assert row.slugs == ["dora"]
