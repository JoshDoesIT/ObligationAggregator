"""Notification dispatch — M2 wires the real channels; the runner already calls this."""

from __future__ import annotations

from sqlalchemy.orm import Session


def dispatch_pending(session: Session) -> int:  # pragma: no cover - replaced in M2
    return 0
