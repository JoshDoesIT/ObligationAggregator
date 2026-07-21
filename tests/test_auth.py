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


# --- Phase 2: API keys, rate limiting, webhooks, invites ---------------------


def _make_key(ml, client, name="ci"):
    csrf = _csrf(client, "/settings")
    r = client.post(
        "/settings/api-keys", data={"name": name, "csrf_token": csrf}, follow_redirects=True
    )
    import re as _re

    m = _re.search(r"won't be shown again:.*?<code[^>]*>([^<]+)</code>", r.text, _re.S)
    assert m, "raw key not surfaced once"
    return m.group(1).strip()


def test_api_key_grants_org_scoped_access(ml):
    from fastapi.testclient import TestClient

    owner = _login_with_org(ml, "owner@keys.test", "KeysOrg")
    raw = _make_key(ml, owner)
    # a fresh client (no cookies) authenticates with the bearer key
    api = TestClient(ml.app)
    h = {"Authorization": f"Bearer {raw}"}
    assert api.get("/api/v1/watchlists", headers=h).status_code == 200
    made = api.post("/api/v1/watchlists", json={"name": "via-key", "channel": "rss"}, headers=h)
    assert made.status_code == 201
    # scoped to the owner's org — visible in the browser session too
    assert "via-key" in owner.get("/api/v1/watchlists").text
    # no key / bad key → 401
    assert api.get("/api/v1/watchlists").status_code == 401
    assert (
        api.get("/api/v1/watchlists", headers={"Authorization": "Bearer oblag_nope"}).status_code
        == 401
    )


def test_api_key_revocation(ml):
    from fastapi.testclient import TestClient

    owner = _login_with_org(ml, "o@rev.test", "RevOrg")
    raw = _make_key(ml, owner)
    api = TestClient(ml.app)
    h = {"Authorization": f"Bearer {raw}"}
    assert api.get("/api/v1/watchlists", headers=h).status_code == 200
    # revoke via UI
    page = owner.get("/settings").text
    import re as _re

    kid = _re.search(r"/settings/api-keys/(\d+)/revoke", page).group(1)
    csrf = _csrf(owner, "/settings")
    owner.post(f"/settings/api-keys/{kid}/revoke", data={"csrf_token": csrf})
    assert api.get("/api/v1/watchlists", headers=h).status_code == 401


def test_api_key_rate_limit(ml, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("OBLAG_API_RATE_LIMIT_PER_MIN", "3")
    from oblag.config import get_settings

    get_settings.cache_clear()
    owner = _login_with_org(ml, "o@rl.test", "RLOrg")
    raw = _make_key(ml, owner)
    api = TestClient(ml.app)
    h = {"Authorization": f"Bearer {raw}"}
    codes = [api.get("/api/v1/watchlists", headers=h).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert 429 in codes[3:]


def test_webhook_ssrf_blocked_and_signed(ml, monkeypatch):
    owner = _login_with_org(ml, "o@wh.test", "WHOrg")
    # SSRF: internal/loopback targets rejected at creation
    bad = owner.post(
        "/api/v1/watchlists",
        json={"name": "evil", "channel": "webhook", "target": "http://169.254.169.254/latest"},
    )
    assert bad.status_code == 422
    localh = owner.post(
        "/api/v1/watchlists",
        json={"name": "evil2", "channel": "webhook", "target": "http://localhost:8000/x"},
    )
    assert localh.status_code == 422

    # a public target is accepted and gets a signing secret; delivery signs the body
    import oblag.netguard as ng

    monkeypatch.setattr(ng, "assert_safe_url", lambda url: None)  # allow example.com in test
    ok = owner.post(
        "/api/v1/watchlists",
        json={"name": "good", "channel": "webhook", "target": "https://hooks.example.com/x"},
    )
    assert ok.status_code == 201

    from oblag.db.models import Event, EventType, Watchlist
    from oblag.db.session import session_scope
    from oblag.notify import _deliver_webhook

    captured = {}

    def fake_post(url, content, headers, timeout, follow_redirects):
        captured["headers"] = headers
        captured["body"] = content

        class R:
            def raise_for_status(self):
                return None

        return R()

    import oblag.notify as notif

    monkeypatch.setattr(notif.httpx, "post", fake_post)
    with session_scope() as db:
        wl = db.query(Watchlist).filter_by(name="good").one()
        ev = Event(type=EventType.item_created, payload={})
        db.add(ev)
        db.flush()
        _deliver_webhook(wl, [(ev, None)])
    assert captured["headers"]["X-Oblag-Signature"].startswith("sha256=")


def test_org_invite_auto_accepts_on_login(ml):
    owner = _login_with_org(ml, "boss@inv.test", "InvOrg")
    csrf = _csrf(owner, "/settings")
    owner.post(
        "/settings/invites",
        data={"email": "teammate@inv.test", "role": "admin", "csrf_token": csrf},
    )
    # invited teammate signs in → joins InvOrg, skips onboarding
    mate = _login(ml, "teammate@inv.test")
    page = mate.get("/watchlists")
    assert page.status_code == 200 and "InvOrg" in page.text
    # they can see the org's watchlists namespace (empty but authorized, not 401)
    assert mate.get("/api/v1/watchlists").status_code == 200


def test_members_only_admin_manages_keys(ml):
    owner = _login_with_org(ml, "owner2@role.test", "RoleOrg")
    csrf = _csrf(owner, "/settings")
    owner.post(
        "/settings/invites",
        data={"email": "member@role.test", "role": "member", "csrf_token": csrf},
    )
    member = _login(ml, "member@role.test")
    # member sees settings read-only: no create-key form
    page = member.get("/settings").text
    assert "Create API key" not in page
    # and a forced POST is a no-op (redirect, no key created)
    csrf2 = _csrf(member, "/settings")
    member.post("/settings/api-keys", data={"name": "x", "csrf_token": csrf2})
    assert "won't be shown again" not in member.get("/settings").text


# --- Phase 3: BYOL isolation, email prefs, assert-date, quotas ---------------


def test_byol_org_isolation(ml, tmp_path):
    a = _login_with_org(ml, "a@byol.test", "ByolA")
    b = _login_with_org(ml, "b@byol.test", "ByolB")
    # org A uploads a licensed doc
    f = tmp_path / "std.txt"
    f.write_text("1.1 Do the thing.\n1.2 Do another thing.\n")
    csrf = _csrf(a, "/byol")
    up = a.post(
        "/byol/upload",
        data={
            "obligation": "pci-dss",
            "version": "v-secret-9",
            "attest_license": "1",
            "csrf_token": csrf,
        },
        files={"file": ("std.txt", f.read_bytes(), "text/plain")},
        follow_redirects=False,
    )
    assert up.status_code == 303
    # A sees its doc
    assert "v-secret-9" in a.get("/byol").text
    # B sees NOTHING of A's (isolation)
    bpage = b.get("/byol").text
    assert "v-secret-9" not in bpage
    # B cannot diff against A's version — no cross-org read
    csrfb = _csrf(b, "/byol")
    r = b.post(
        "/byol/diff",
        data={
            "obligation": "pci-dss",
            "from_version": "1.0",
            "to_version": "1.0",
            "csrf_token": csrfb,
        },
    )
    assert "no BYOL document" in r.text


def test_org_email_preferences_saved_and_applied(ml, monkeypatch):
    owner = _login_with_org(ml, "o@mail.test", "MailOrg")
    csrf = _csrf(owner, "/settings")
    owner.post(
        "/settings/email",
        data={
            "notify_from_name": "MailOrg Alerts",
            "notify_reply_to": "reply@mail.test",
            "csrf_token": csrf,
        },
    )
    from oblag.db.models import Org
    from oblag.db.session import session_scope

    with session_scope() as db:
        org = db.query(Org).filter_by(name="MailOrg").one()
        assert org.notify_from_name == "MailOrg Alerts"
        assert org.notify_reply_to == "reply@mail.test"


def test_assert_date_admin_only(ml, seeded_via_ml=None):
    from oblag.db.models import PipelineItem
    from oblag.db.session import session_scope

    # seed one item to assert against
    with session_scope() as db:
        db.add(
            PipelineItem(
                source_system="test",
                jurisdiction="US",
                title="t",
                state="proposed",
                track="proposed",
            )
        )
    admin = _login(ml, "admin@oblag.test")  # instance admin (from fixture env)
    csrf = _csrf(admin, "/settings")
    admin.post(
        "/auth/onboarding",
        data={"org_name": "AdminOrg", "csrf_token": _csrf(admin, "/auth/onboarding")},
    ) if admin.get("/watchlists", follow_redirects=False).status_code == 303 else None
    with session_scope() as db:
        iid = db.query(PipelineItem.id).first()[0]
    # admin can assert a date
    csrf = _csrf(admin, f"/items/{iid}")
    r = admin.post(
        f"/items/{iid}/assert-date",
        data={
            "date_type": "comment_close",
            "value": "2027-01-01",
            "confidence": "published_firm",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # a non-admin cannot even see the form
    member = _login_with_org(ml, "plain@user.test", "PlainOrg")
    assert "assert-date" not in member.get(f"/items/{iid}").text


def test_watchlist_quota_enforced(ml, monkeypatch):
    monkeypatch.setenv("OBLAG_QUOTA_WATCHLISTS", "2")
    from oblag.config import get_settings

    get_settings.cache_clear()
    c = _login_with_org(ml, "q@quota.test", "QuotaOrg")
    assert c.post("/api/v1/watchlists", json={"name": "a", "channel": "rss"}).status_code == 201
    assert c.post("/api/v1/watchlists", json={"name": "b", "channel": "rss"}).status_code == 201
    over = c.post("/api/v1/watchlists", json={"name": "c", "channel": "rss"})
    assert over.status_code == 409
