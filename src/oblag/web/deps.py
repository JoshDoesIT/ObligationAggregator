from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from oblag.db.session import get_session_factory


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
