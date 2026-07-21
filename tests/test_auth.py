from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def ml(engine, db, monkeypatch):
    """App in magic-link mode, with sent login URLs captured instead of emailed."""
    import oblag.db.session as dbsession

    monkeypatch.setattr(dbsession, "_engine", engine)
    monkeypatch.setattr(
        dbsession, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    monkeypatch.setenv("OBLAG_AUTH", "magic-link")
    monkeypatch.setenv("OBLAG_BASE_URL", "http://testserver")
    monkeypatch.setenv("OBLAG_INSTANCE_ADMINS", "admin@oblag.test")
    from oblag.config import get_settings

    get_settings.cache_clear()
    sent: list[tuple[str, str]] = []
    import oblag.auth as authmod

    monkeypatch.setattr(authmod, "send_login_email", lambda to, url: sent.append((to, url)))

    from oblag.web.app import create_app

    return SimpleNamespace(app=create_app(), sent=sent)


def _csrf(client, path: str) -> str:
    html = client.get(path).text
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"no csrf token on {path}"
    return m.group(1)


def _login(ml, email: str):
    from fastapi.testclient import TestClient

    c = TestClient(ml.app)
    c.post("/auth/login", data={"email": email})
    url = ml.sent[-1][1]
    token = url.split("token=")[1]
    c.get(f"/auth/verify?token={token}", follow_redirects=False)
    return c


def _login_with_org(ml, email: str, org_name: str):
    c = _login(ml, email)
    csrf = _csrf(c, "/auth/onboarding")
    c.post("/auth/onboarding", data={"org_name": org_name, "csrf_token": csrf})
    return c


# --- backward compatibility: single-org (disabled) mode ----------------------


def test_disabled_mode_watchlists_open_no_login(client, seeded):
    # existing behavior: no auth wall, watchlists usable, no sign-in UI
    r = client.get("/watchlists")
    assert r.status_code == 200
    assert "Sign in" not in r.text
    created = client.post("/api/v1/watchlists", json={"name": "wl", "channel": "rss"})
    assert created.status_code == 201


# --- magic-link mode: gating -------------------------------------------------


def test_public_pages_stay_public_in_magic_link_mode(ml):
    from fastapi.testclient import TestClient

    c = TestClient(ml.app)
    for path in ("/", "/obligations", "/deadlines", "/events", "/health"):
        assert c.get(path).status_code == 200
    assert "Sign in" in c.get("/").text


def test_watchlists_requires_login(ml):
    from fastapi.testclient import TestClient

    c = TestClient(ml.app)
    r = c.get("/watchlists", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/login"
    # unauthenticated API is 401, not open
    assert c.get("/api/v1/watchlists").status_code == 401


def test_no_account_enumeration(ml):
    from fastapi.testclient import TestClient

    c = TestClient(ml.app)
    known = c.post("/auth/login", data={"email": "someone@x.com"})
    unknown = c.post("/auth/login", data={"email": "nobody@y.com"})
    assert known.status_code == unknown.status_code == 200
    assert "Check your email" in known.text and "Check your email" in unknown.text


# --- login flow + org onboarding ---------------------------------------------


def test_full_login_and_org_scoped_watchlist(ml):
    c = _login(ml, "founder@acme.test")
    # first login → onboarding (no org yet)
    assert c.get("/watchlists", follow_redirects=False).headers["location"] == "/auth/onboarding"
    csrf = _csrf(c, "/auth/onboarding")
    c.post("/auth/onboarding", data={"org_name": "Acme", "csrf_token": csrf})
    # now watchlists works and is scoped to the new org
    page = c.get("/watchlists")
    assert page.status_code == 200 and "Acme" in page.text
    r = c.post("/api/v1/watchlists", json={"name": "acme wl", "channel": "rss"})
    assert r.status_code == 201
    assert [w["name"] for w in c.get("/api/v1/watchlists").json()["watchlists"]] == ["acme wl"]


def test_org_isolation(ml):
    a = _login_with_org(ml, "a@one.test", "OrgOne")
    b = _login_with_org(ml, "b@two.test", "OrgTwo")
    made = a.post("/api/v1/watchlists", json={"name": "secret", "channel": "rss"}).json()
    wl_id = made["id"]
    # B cannot see A's watchlist...
    assert b.get("/api/v1/watchlists").json()["watchlists"] == []
    # ...nor delete it (404, never revealed)
    assert b.delete(f"/api/v1/watchlists/{wl_id}").status_code == 404
    # A still can
    assert a.delete(f"/api/v1/watchlists/{wl_id}").status_code == 204


def test_invalid_and_reused_token_rejected(ml):
    from fastapi.testclient import TestClient

    c = TestClient(ml.app)
    assert "invalid or expired" in c.get("/auth/verify?token=garbage").text
    c.post("/auth/login", data={"email": "x@z.test"})
    token = ml.sent[-1][1].split("token=")[1]
    assert c.get(f"/auth/verify?token={token}", follow_redirects=False).status_code == 303
    # single-use: a fresh client replaying the same token is rejected
    c2 = TestClient(ml.app)
    assert "invalid or expired" in c2.get(f"/auth/verify?token={token}").text


def test_csrf_required_for_form_posts(ml):
    c = _login_with_org(ml, "c@three.test", "OrgThree")
    # HTML form POST without a CSRF token is rejected
    bad = c.post("/watchlists", data={"name": "x", "channel": "rss"}, follow_redirects=False)
    assert bad.status_code == 403
    csrf = _csrf(c, "/watchlists")
    ok = c.post(
        "/watchlists",
        data={"name": "x", "channel": "rss", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert ok.status_code == 303


def test_logout_ends_session(ml):
    c = _login_with_org(ml, "d@four.test", "OrgFour")
    assert c.get("/api/v1/watchlists").status_code == 200
    csrf = _csrf(c, "/watchlists")
    c.post("/auth/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert c.get("/api/v1/watchlists").status_code == 401


def test_instance_admin_flag(ml):
    from oblag.auth import is_instance_admin

    assert is_instance_admin("admin@oblag.test") is True
    assert is_instance_admin("ADMIN@OBLAG.TEST") is True
    assert is_instance_admin("random@x.test") is False
