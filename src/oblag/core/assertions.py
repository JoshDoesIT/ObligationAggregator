from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from oblag.core.reducer import current_dates
from oblag.db.models import Confidence, DateType, Event, EventType, KeyDate

"""Curated date assertions: the mechanism for dates that come from humans or
sources without an adapter yet (e.g. Unified Agenda projected-final dates).
Same append-only semantics as the reducer (spec 00 invariant 1)."""


def assert_date(
    session: Session,
    item_id: int,
    date_type: DateType,
    value: date,
    confidence: Confidence,
    label: str | None = None,
    source_snapshot_id: int | None = None,
    note: str | None = None,
) -> Event | None:
    """Assert a date; supersedes any current assertion. Returns the date_changed event,
    or None when the assertion matches the current value (no-op)."""
    live = current_dates(session, item_id)
    cur = live.get((date_type, label))
    if cur is not None and cur.value == value and cur.confidence == confidence:
        return None
    row = KeyDate(
        pipeline_item_id=item_id,
        date_type=date_type,
        label=label,
        value=value,
        confidence=confidence,
        source_snapshot_id=source_snapshot_id,
        supersedes_id=cur.id if cur else None,
    )
    session.add(row)
    session.flush()
    payload = {
        "date_type": date_type.value,
        "label": label,
        "from": cur.value.isoformat() if cur else None,
        "to": value.isoformat(),
        "confidence": confidence.value,
        "superseded_key_date_id": cur.id if cur else None,
    }
    if note:
        payload["note"] = note
    ev = Event(
        pipeline_item_id=item_id,
        type=EventType.date_changed,
        payload=payload,
        snapshot_id=source_snapshot_id,
    )
    session.add(ev)
    session.flush()
    return ev
