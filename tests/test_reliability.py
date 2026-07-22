from __future__ import annotations

import oblag.db.session as dbsession
from oblag.catalog import seed_obligations
from oblag.db.models import AdapterHealth, KVMeta


def _wire_app(engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(dbsession, "_engine", engine)
    monkeypatch.setattr(
        dbsession, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )


def test_engine_pre_ping_for_postgres(monkeypatch):
    """Non-sqlite engines get pool_pre_ping + pool_recycle (the fix for Neon's
    'SSL connection has been closed unexpectedly' seen live)."""
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured.update(kwargs)
        from sqlalchemy import create_engine as real

        return real("sqlite://")

    monkeypatch.setattr(dbsession, "_engine", None)
    monkeypatch.setattr(dbsession, "create_engine", fake_create_engine)
    monkeypatch.setattr(
        dbsession, "get_settings", lambda: type("S", (), {"database_url": "postgresql://x/y"})()
    )
    dbsession.get_engine()
    assert captured.get("pool_pre_ping") is True
    assert captured.get("pool_recycle") == 300
    monkeypatch.setattr(dbsession, "_engine", None)


def test_boot_runs_once_per_version_then_fast_path(engine, monkeypatch):
    from oblag.web import app as appmod

    _wire_app(engine, monkeypatch)
    calls = {"sync": 0}
    monkeypatch.setattr(
        appmod, "_sync_catalog", lambda: calls.__setitem__("sync", calls["sync"] + 1)
    )
    monkeypatch.setattr(appmod, "_provision_tenancy", lambda: None)
    monkeypatch.setattr(appmod, "_repair_data", lambda: None)
    monkeypatch.setattr(appmod, "_seed_milestones", lambda: None)

    appmod.create_app()
    assert calls["sync"] == 1  # first boot ran the work
    with dbsession.session_scope() as s:
        assert s.get(KVMeta, "boot_version") is not None

    appmod.create_app()  # warm cold-start, same version
    assert calls["sync"] == 1  # fast path: boot work skipped

    # a new deployment version re-runs the boot work
    monkeypatch.setattr(appmod, "__version__", "999.0.0")
    appmod.create_app()
    assert calls["sync"] == 2


def test_preview_env_skips_mutating_boot(engine, monkeypatch):
    from oblag.web import app as appmod

    _wire_app(engine, monkeypatch)
    monkeypatch.setenv("VERCEL_ENV", "preview")
    from oblag.config import get_settings

    get_settings.cache_clear()
    ran = {"sync": False}
    monkeypatch.setattr(appmod, "_sync_catalog", lambda: ran.__setitem__("sync", True))
    monkeypatch.setattr(appmod, "_provision_tenancy", lambda: None)
    monkeypatch.setattr(appmod, "_repair_data", lambda: None)
    monkeypatch.setattr(appmod, "_seed_milestones", lambda: None)

    appmod.create_app()
    assert ran["sync"] is False  # preview must not mutate the (possibly prod) DB
    with dbsession.session_scope() as s:
        assert s.get(KVMeta, "boot_version") is None  # not stamped → re-checked each boot
    get_settings.cache_clear()


def test_ops_alert_emails_unhealthy_adapters_once_per_day(db, monkeypatch):
    import oblag.notify as notify

    seed_obligations(db)
    db.add(AdapterHealth(adapter="cellar", consecutive_failures=3, last_error="boom\ntrace"))
    db.add(AdapterHealth(adapter="edpb", consecutive_failures=1))  # below threshold
    db.commit()

    sent = []
    monkeypatch.setattr(
        notify, "_send_plain_email", lambda to, subj, body: sent.append((to, subj, body))
    )
    monkeypatch.setattr(
        notify,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "smtp_host": "smtp.x",
                "smtp_port": 587,
                "smtp_user": None,
                "smtp_password": None,
                "smtp_from": "ops@x.com",
                "ops_alert_emails": "",
                "instance_admins": "admin@x.com",
                "base_url": "https://x",
            },
        )(),
    )

    assert notify.alert_unhealthy_adapters(db) == 1
    assert len(sent) == 1
    to, subj, body = sent[0]
    assert to == ["admin@x.com"] and "cellar" in body and "edpb" not in body
    # same day → no repeat
    assert notify.alert_unhealthy_adapters(db) == 0
    assert len(sent) == 1


def test_ops_alert_noop_without_smtp(db, monkeypatch):
    import oblag.notify as notify

    db.add(AdapterHealth(adapter="cellar", consecutive_failures=5))
    db.commit()
    monkeypatch.setattr(
        notify,
        "get_settings",
        lambda: type(
            "S",
            (),
            {"smtp_host": None, "ops_alert_emails": "", "instance_admins": "", "smtp_from": ""},
        )(),
    )
    assert notify.alert_unhealthy_adapters(db) == 0
