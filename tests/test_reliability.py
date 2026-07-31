from __future__ import annotations

from datetime import date

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
    # The real Settings, not a hand-rolled stand-in: a stub with a hand-picked subset
    # of fields silently breaks every time config gains one, which is exactly what
    # happened when the mail backend was added.
    from oblag.config import Settings

    monkeypatch.setattr(
        notify,
        "get_settings",
        lambda: Settings(
            smtp_host="smtp.x",
            smtp_from="ops@x.com",
            ops_alert_emails="",
            instance_admins="admin@x.com",
            base_url="https://x",
        ),
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
    from oblag.config import Settings

    monkeypatch.setattr(
        notify,
        "get_settings",
        lambda: Settings(
            smtp_host=None,
            resend_api_key=None,
            ops_alert_emails="",
            instance_admins="",
            smtp_from="",
        ),
    )
    assert notify.alert_unhealthy_adapters(db) == 0


# --- rebuild: refetch the record instead of hand-editing the database ---


def _seed_for_rebuild(db, monkeypatch):
    """Two items from a fake source, one carrying a hand-typed deadline."""
    from oblag.adapters.base import NormalizedDate, NormalizedItem
    from oblag.core.assertions import assert_date
    from oblag.core.reducer import reduce_item
    from oblag.db.models import Confidence, DateType

    a = reduce_item(
        db,
        NormalizedItem(
            source_system="federal_register",
            external_key=("fr_doc_number", "2024-00001"),
            jurisdiction="US-Federal",
            title="MANGLED - title the parser broke",
            native_status="PRORULE",
            track="proposed",
            dates=[NormalizedDate(DateType.comment_close, date(2026, 9, 1), Confidence.derived)],
        ),
    )
    b = reduce_item(
        db,
        NormalizedItem(
            source_system="federal_register",
            external_key=("fr_doc_number", "2024-00002"),
            jurisdiction="US-Federal",
            title="Gone from the source",
            native_status="PRORULE",
            track="proposed",
        ),
    )
    # a human typed this one; no feed carries it
    assert_date(
        db,
        a.item.id,
        DateType.effective,
        date(2027, 1, 1),
        Confidence.published_firm,
        note="from the PDF",
    )
    db.commit()
    return a.item.id, b.item.id


def test_rebuild_refreshes_then_retires_what_the_source_dropped(db, monkeypatch):
    """A rebuild re-reads the record: rows the source still lists are refreshed in
    place (so a parser fix finally reaches them), and rows an enumerating source
    stopped listing are retired."""
    from oblag.db.models import PipelineItem
    from oblag.rebuild import rebuild

    _seed_for_rebuild(db, monkeypatch)

    def fake_run(session, name, window=None, **kw):
        """The source now lists item A with a clean title, and no longer lists B."""
        from oblag.adapters.base import NormalizedItem
        from oblag.core.reducer import reduce_item
        from oblag.core.runner import RunStats

        reduce_item(
            session,
            NormalizedItem(
                source_system="federal_register",
                external_key=("fr_doc_number", "2024-00001"),
                jurisdiction="US-Federal",
                title="Clean title straight from the record",
                native_status="PRORULE",
                track="proposed",
            ),
        )
        return RunStats(adapter=name, pages=1, items=1, created=0)

    monkeypatch.setattr(
        "oblag.rebuild.backfill_plan", lambda d, a=None: [("federal_register", None)]
    )
    monkeypatch.setattr("oblag.core.runner.run_adapter", fake_run)

    report = rebuild(db, days=30)

    titles = [t for (t,) in db.query(PipelineItem.title).all()]
    # the mangled row was REFRESHED, not deleted and re-created: the curated date it
    # carries is still attached, which a wipe would have had to restore by hand
    assert "MANGLED - title the parser broke" not in titles
    assert "Clean title straight from the record" in titles
    survivor = db.query(PipelineItem).filter_by(title="Clean title straight from the record").one()
    assert any(kd.value == date(2027, 1, 1) for kd in survivor.key_dates)
    # the item the source stopped listing was retired
    assert "Gone from the source" not in titles
    assert report.purged["pruned"]


def test_rebuild_keeps_what_it_could_not_refetch(db, monkeypatch):
    """The hazard a blind wipe hides: a source that fails (iso.org served 403 in the
    first live trial) must keep every row it gave us before, and an item a human
    annotated is never retired even when its source drops it."""
    from oblag.adapters.base import NormalizedItem
    from oblag.core.assertions import assert_date
    from oblag.core.reducer import reduce_item
    from oblag.db.models import Confidence, DateType, PipelineItem
    from oblag.rebuild import rebuild

    iso = reduce_item(
        db,
        NormalizedItem(
            source_system="iso_catalog",
            external_key=("iso_std", "27001"),
            jurisdiction="Global",
            title="ISO/IEC 27001:2022",
            native_status="published",
            track="final",
        ),
    )
    annotated = reduce_item(
        db,
        NormalizedItem(
            source_system="hitrust",
            external_key=("hitrust_release", "11.0.0"),
            jurisdiction="Global",
            title="HITRUST CSF v11.0.0",
            native_status="release",
            track="final",
        ),
    )
    assert_date(
        db,
        annotated.item.id,
        DateType.effective,
        date(2026, 5, 5),
        Confidence.published_firm,
        note="typed from the advisory PDF",
    )
    db.commit()

    def fake_run(session, name, window=None, **kw):
        from oblag.core.runner import RunStats

        if name == "iso_catalog":
            raise RuntimeError("403 Forbidden")
        return RunStats(adapter=name, pages=1, items=0, created=0)

    monkeypatch.setattr(
        "oblag.rebuild.backfill_plan",
        lambda d, a=None: [("iso_catalog", None), ("hitrust", None)],
    )
    monkeypatch.setattr("oblag.core.runner.run_adapter", fake_run)

    report = rebuild(db, days=30)

    assert any("iso_catalog" in e for e in report.errors)
    # the unreachable source keeps its rows
    assert db.get(PipelineItem, iso.item.id) is not None
    # hitrust answered and lists nothing, but this row carries a human's date
    assert db.get(PipelineItem, annotated.item.id) is not None
    assert report.purged["kept_curated"] == 1


def test_rebuild_keeps_tenancy_and_catalog(db, monkeypatch):
    """Orgs, watchlists and the obligation catalog are not adapter output: a rebuild
    must never touch them."""
    from oblag.db.models import Obligation, Org, Watchlist
    from oblag.rebuild import rebuild

    seed_obligations(db)
    _seed_for_rebuild(db, monkeypatch)
    org = Org(slug="acme", name="Acme")
    db.add(org)
    db.flush()
    db.add(Watchlist(name="keep me", channel="rss", filters={}, org_id=org.id))
    db.commit()
    obligations_before = db.query(Obligation).count()

    monkeypatch.setattr("oblag.rebuild.backfill_plan", lambda days, adapters=None: [])
    rebuild(db, days=30)

    assert db.query(Watchlist).filter_by(name="keep me").count() == 1
    assert db.query(Org).filter_by(name="Acme").count() == 1
    assert db.query(Obligation).count() == obligations_before


def test_backfill_plan_windows_only_sources_that_accept_them():
    """Windowed sources get one slice per quarter so a rebuild can reach back years;
    feed sources get a single run, because they only ever serve what they carry now."""
    from oblag.rebuild import WINDOW_DAYS, backfill_plan

    plan = backfill_plan(365, ["federal_register", "pci_ssc"])
    fr = [w for name, w in plan if name == "federal_register"]
    feed = [w for name, w in plan if name == "pci_ssc"]
    assert feed == [None]
    assert len(fr) >= 365 // WINDOW_DAYS
    assert all(w is not None for w in fr)
    # oldest first, so the feed fills in chronological order
    assert fr == sorted(fr)
    assert (fr[-1][1] - fr[0][0]).days >= 364


def test_catchup_resumes_across_runs_and_stops_when_done(db, monkeypatch):
    """The daily cron drains the historical backfill a slice at a time, so a deployment
    fills in its own past with nobody handling the cron secret. Progress survives
    between invocations, and once the plan is finished it never runs again."""
    from oblag.rebuild import catchup

    calls: list[tuple] = []

    def fake_run(session, name, window=None, **kw):
        from oblag.core.runner import RunStats

        calls.append((name, window))
        return RunStats(adapter=name, pages=1, items=1, created=1)

    plan = [("federal_register", ("a", "b")), ("federal_register", ("c", "d")), ("pci_ssc", None)]
    monkeypatch.setattr("oblag.rebuild.backfill_plan", lambda d, a=None: plan)
    monkeypatch.setattr("oblag.core.runner.run_adapter", fake_run)

    # a budget of zero still makes one step of progress, so the queue can never stall
    first = catchup(db, days=730, budget_s=0)
    db.commit()
    assert first["status"] == "in_progress" and first["step"] == 1

    second = catchup(db, days=730)  # no budget: finishes the rest
    db.commit()
    assert second["status"] == "done" and second["step"] == len(plan)
    assert len(calls) == len(plan), "every slice ran exactly once across the two runs"

    # finished means finished: a later cron does no work at all
    again = catchup(db, days=730)
    assert again["status"] == "already_done"
    assert len(calls) == len(plan)

    # asking for a different depth re-arms it
    monkeypatch.setattr("oblag.rebuild.backfill_plan", lambda d, a=None: plan[:1])
    assert catchup(db, days=365)["status"] == "done"


def test_catchup_survives_a_dead_source(db, monkeypatch):
    """One unreachable source must not stall the queue behind it."""
    from oblag.rebuild import catchup

    def fake_run(session, name, window=None, **kw):
        from oblag.core.runner import RunStats

        if name == "iso_catalog":
            raise RuntimeError("403 Forbidden")
        return RunStats(adapter=name, pages=1, items=0, created=0)

    monkeypatch.setattr(
        "oblag.rebuild.backfill_plan",
        lambda d, a=None: [("iso_catalog", None), ("pci_ssc", None)],
    )
    monkeypatch.setattr("oblag.core.runner.run_adapter", fake_run)

    result = catchup(db, days=730)
    assert result["status"] == "done"
    assert any("iso_catalog" in e for e in result["errors"])
    assert result["ran"] == 1  # the source behind it still ran


def _stub_adapter(docs, items_for):
    from oblag.adapters.base import SourceAdapter

    class Stub(SourceAdapter):
        name = "stub"
        jurisdiction = "Global"

        def fetch_raw(self, ctx):
            return list(docs)

        def normalize(self, raw):
            return items_for(raw)

    return Stub()


def test_a_watched_page_that_stops_matching_lands_on_adapter_health(db, monkeypatch):
    """The failure this closes: DFS rebuilt its site, the sentence standard_pages parsed
    disappeared, and the row simply stopped updating while still serving what it last
    said. The fetch was a 200, the run was a success, nothing anywhere said otherwise.
    A page an adapter declares it expects an item from must not fail silently."""
    from oblag.adapters.base import NormalizedItem, RawDocument
    from oblag.core import runner

    good = RawDocument(url="https://x/ok", content=b"a", meta={"page": "ok", "expect_item": "1"})
    blind = RawDocument(
        url="https://x/gone", content=b"b", meta={"page": "gone", "expect_item": "1"}
    )

    def items_for(raw):
        if raw.meta["page"] != "ok":
            return []
        return [
            NormalizedItem(
                source_system="stub",
                external_key=("watched_page", "ok"),
                jurisdiction="Global",
                title="Something v1",
                native_status="current",
            )
        ]

    monkeypatch.setattr(runner, "get_adapter", lambda n: _stub_adapter([good, blind], items_for))
    stats = runner.run_adapter(db, "stub")

    assert stats.blind == ["gone: nothing matched"]
    health = db.query(AdapterHealth).filter_by(adapter="stub").one()
    assert health.last_error == "gone: nothing matched"
    # the run still succeeded: the page that DID match produced its item, and a blind
    # page must not trip the failure counter that self-disables an adapter
    assert stats.items == 1
    assert health.consecutive_failures == 0
    assert health.last_success_at is not None


def test_a_clean_run_clears_a_previous_blind_warning(db, monkeypatch):
    from oblag.adapters.base import NormalizedItem, RawDocument
    from oblag.core import runner

    doc = RawDocument(url="https://x/ok", content=b"a", meta={"page": "ok", "expect_item": "1"})
    seen = {"n": 0}

    def items_for(raw):
        seen["n"] += 1
        if seen["n"] == 1:
            return []
        return [
            NormalizedItem(
                source_system="stub",
                external_key=("watched_page", "ok"),
                jurisdiction="Global",
                title="Something v1",
                native_status="current",
            )
        ]

    monkeypatch.setattr(runner, "get_adapter", lambda n: _stub_adapter([doc], items_for))
    runner.run_adapter(db, "stub")
    assert db.query(AdapterHealth).filter_by(adapter="stub").one().last_error
    runner.run_adapter(db, "stub")
    assert db.query(AdapterHealth).filter_by(adapter="stub").one().last_error is None


def test_pages_that_never_promised_an_item_are_not_flagged(db, monkeypatch):
    """Most adapters page through listings where an empty page is ordinary."""
    from oblag.adapters.base import RawDocument
    from oblag.core import runner

    doc = RawDocument(url="https://x/page2", content=b"[]", meta={})
    monkeypatch.setattr(runner, "get_adapter", lambda n: _stub_adapter([doc], lambda raw: []))
    stats = runner.run_adapter(db, "stub")
    assert stats.blind == []
    assert db.query(AdapterHealth).filter_by(adapter="stub").one().last_error is None
