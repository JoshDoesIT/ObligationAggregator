from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from oblag.config import get_settings
from oblag.db.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _configure_sqlite(engine: Engine) -> None:
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_pragma(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        if url.startswith("sqlite:///"):
            db_path = Path(url.removeprefix("sqlite:///"))
            if db_path.parent != Path("."):
                db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url)
        _configure_sqlite(_engine)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


_TEXT_UPGRADES = [
    ("obligation", "canonical_url"),
    ("pipeline_item", "url"),
    ("join_key", "value"),
    ("snapshot", "source_url"),
    ("snapshot", "storage_ref"),
    ("snapshot", "attestation_ref"),
    ("watchlist", "target"),
]


def init_db(engine: Engine | None = None) -> None:
    eng = engine or get_engine()
    Base.metadata.create_all(eng)
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sql_text

    if eng.dialect.name == "postgresql":
        # Databases created before v0.1.2 have varchar limits on URL-bearing columns
        # (SQLite never enforced them; Postgres does — observed live: CELLAR SPARQL
        # source URLs exceed 1024 chars). ALTER ... TYPE TEXT is idempotent.
        with eng.begin() as conn:
            for table, column in _TEXT_UPGRADES:
                conn.execute(sql_text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TEXT"))
    # v0.1.7: key_date.retracted (create_all only builds new tables, not new columns)
    cols = {c["name"] for c in sa_inspect(eng).get_columns("key_date")}
    if "retracted" not in cols:
        default = "FALSE" if eng.dialect.name == "postgresql" else "0"
        with eng.begin() as conn:
            conn.execute(
                sql_text(f"ALTER TABLE key_date ADD COLUMN retracted BOOLEAN DEFAULT {default}")
            )
            conn.execute(sql_text(f"UPDATE key_date SET retracted = {default}"))
    # v0.2.0 (spec 07): watchlist.org_id — new tenancy column on an existing table
    wl_cols = {c["name"] for c in sa_inspect(eng).get_columns("watchlist")}
    if "org_id" not in wl_cols:
        with eng.begin() as conn:
            conn.execute(sql_text("ALTER TABLE watchlist ADD COLUMN org_id INTEGER"))
    # v0.3.0 (spec 07 Phase 2): watchlist.signing_secret for webhook HMAC
    if "signing_secret" not in wl_cols:
        with eng.begin() as conn:
            conn.execute(sql_text("ALTER TABLE watchlist ADD COLUMN signing_secret VARCHAR(64)"))
    # v0.4.0 (spec 07 Phase 3): BYOL org isolation + per-org email prefs
    pd_cols = {c["name"] for c in sa_inspect(eng).get_columns("private_document")}
    if "org_id" not in pd_cols:
        with eng.begin() as conn:
            conn.execute(sql_text("ALTER TABLE private_document ADD COLUMN org_id INTEGER"))
    org_cols = {c["name"] for c in sa_inspect(eng).get_columns("org")}
    for col in ("notify_from_name", "notify_reply_to"):
        if col not in org_cols:
            with eng.begin() as conn:
                conn.execute(sql_text(f"ALTER TABLE org ADD COLUMN {col} VARCHAR(320)"))
    # v0.4.2: obligation.current_version — which version of a standard is in force
    ob_cols = {c["name"] for c in sa_inspect(eng).get_columns("obligation")}
    if "current_version" not in ob_cols:
        with eng.begin() as conn:
            conn.execute(sql_text("ALTER TABLE obligation ADD COLUMN current_version VARCHAR(64)"))


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_for_tests() -> None:
    """Clear cached engine/factory so tests can point OBLAG_DATABASE_URL elsewhere."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
    get_settings.cache_clear()
