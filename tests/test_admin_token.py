from __future__ import annotations

import hashlib


def _csrf(token: str) -> str:
    return hashlib.sha256(f"csrf:{token}".encode()).hexdigest()


def test_open_mode_allows_assert_date(client, seeded, db):
    """No OBLAG_ADMIN_TOKEN → the convenient open default is preserved."""
    from oblag.db.models import PipelineItem

    item = db.query(PipelineItem).filter_by(source_system="federal_register").first()
    r = client.post(
        f"/items/{item.id}/assert-date",
        data={"date_type": "effective", "value": "2027-01-01", "confidence": "published_firm"},
        follow_redirects=False,
    )
    assert r.status_code == 303  # accepted
    assert client.get("/admin/unlock").status_code == 404  # no gate configured


def _token_client(client, monkeypatch, token="s3cr3t-op"):
    monkeypatch.setenv("OBLAG_ADMIN_TOKEN", token)
    from oblag.config import get_settings

    get_settings.cache_clear()
    return client


def test_admin_token_gates_shared_writes(client, seeded, db, monkeypatch):
    from oblag.db.models import PipelineItem

    token = "s3cr3t-op"
    c = _token_client(client, monkeypatch, token)
    item = db.query(PipelineItem).filter_by(source_system="federal_register").first()

    # locked: anonymous visitor is not admin, cannot assert dates, sees no admin form
    body = c.get(f"/items/{item.id}").text
    assert "Add a curated date" not in body
    r = c.post(
        f"/items/{item.id}/assert-date",
        data={"date_type": "effective", "value": "2027-01-01", "confidence": "published_firm"},
        follow_redirects=False,
    )
    assert r.status_code == 403

    # wrong token is rejected; correct token unlocks (sets the cookie)
    assert (
        c.post("/admin/unlock", data={"token": "nope"}, follow_redirects=False).status_code == 403
    )
    assert c.post("/admin/unlock", data={"token": token}, follow_redirects=False).status_code == 303

    # unlocked: the form appears and the write succeeds with the derived CSRF token
    assert "Add a curated date" in c.get(f"/items/{item.id}").text
    ok = c.post(
        f"/items/{item.id}/assert-date",
        data={
            "date_type": "effective",
            "value": "2027-01-01",
            "confidence": "published_firm",
            "csrf_token": _csrf(token),
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303
    # a forged/absent CSRF token is rejected even when unlocked
    bad = c.post(
        f"/items/{item.id}/assert-date",
        data={"date_type": "effective", "value": "2028-01-01", "confidence": "published_firm"},
        follow_redirects=False,
    )
    assert bad.status_code == 403
    get_settings_clear(monkeypatch)


def test_cdn_cache_skipped_for_unlocked_operator(client, seeded, monkeypatch):
    token = "s3cr3t-op"
    c = _token_client(client, monkeypatch, token)
    # anonymous (cookieless) read is CDN-cacheable
    assert "max-age=60" in c.get("/").headers.get("vercel-cdn-cache-control", "")
    c.post("/admin/unlock", data={"token": token}, follow_redirects=False)
    # once the admin cookie is present, the response must NOT be publicly cached
    assert "max-age=60" not in c.get("/").headers.get("vercel-cdn-cache-control", "")
    get_settings_clear(monkeypatch)


def get_settings_clear(monkeypatch):
    from oblag.config import get_settings

    monkeypatch.delenv("OBLAG_ADMIN_TOKEN", raising=False)
    get_settings.cache_clear()
