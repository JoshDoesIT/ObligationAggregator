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
