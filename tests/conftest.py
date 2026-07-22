from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from oblag.db.models import Base

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def engine():
    # OBLAG_TEST_DATABASE_URL runs the suite against a real Postgres (CI parity job) to
    # catch dialect-only bugs SQLite hides (JSON equality, varchar overflow, SSL) — all
    # three prod-only failures observed live. Fresh schema per test for isolation.
    pg_url = os.environ.get("OBLAG_TEST_DATABASE_URL")
    if pg_url:
        eng = create_engine(pg_url)
        Base.metadata.drop_all(eng)
        Base.metadata.create_all(eng)
        yield eng
        Base.metadata.drop_all(eng)
        eng.dispose()
        return
    # StaticPool: one shared connection so the TestClient's worker threads see the same
    # in-memory database as the test's own session.
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


@pytest.fixture()
def snapshot_root(tmp_path: Path) -> Path:
    return tmp_path / "snapshots"


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep tests hermetic: no real .env, data dir under tmp."""
    monkeypatch.setenv("OBLAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OBLAG_DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'test.db'}")
    monkeypatch.chdir(tmp_path) if os.environ.get("OBLAG_TEST_CHDIR") else None
    from oblag.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def load_fixture(*parts: str) -> bytes:
    return (FIXTURES.joinpath(*parts)).read_bytes()


@pytest.fixture()
def client(engine, db, monkeypatch):
    """TestClient wired to the test engine."""
    import oblag.db.session as dbsession

    monkeypatch.setattr(dbsession, "_engine", engine)
    monkeypatch.setattr(
        dbsession, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    from fastapi.testclient import TestClient

    from oblag.web.app import create_app

    return TestClient(create_app())


@pytest.fixture()
def seeded(db):
    """Catalog + one CIRCIA-like item with a future comment_close."""
    from datetime import date, timedelta

    from oblag.adapters.base import NormalizedDate, NormalizedItem
    from oblag.catalog import seed_obligations
    from oblag.core.reducer import reduce_item
    from oblag.db.models import Confidence, DateType

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


@pytest.fixture()
def scope_off(monkeypatch):
    """Disable the security/privacy relevance gate for tests exercising other
    mechanics on fixtures that contain out-of-scope documents."""
    from oblag.config import get_settings

    monkeypatch.setenv("OBLAG_SCOPE_FILTER", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
