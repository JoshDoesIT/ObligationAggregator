"""Data-repair helpers shared by the internal maintenance endpoints and boot fixes."""

from __future__ import annotations

from sqlalchemy.orm import Session

from oblag.db.models import Event, JoinKey, KeyDate, NotificationLog, PipelineItem


def purge_items(db: Session, item_ids: list[int]) -> dict[str, int | list[int]]:
    """Hard-delete pipeline items with their dates, join keys, events and notification
    rows, and unlink any survivor that resolved to them. The next ingestion run
    re-creates the items cleanly if the source still carries them."""
    found = [i for (i,) in db.query(PipelineItem.id).filter(PipelineItem.id.in_(item_ids)).all()]
    event_ids = [e for (e,) in db.query(Event.id).filter(Event.pipeline_item_id.in_(found)).all()]
    deleted_notifications = 0
    if event_ids:
        deleted_notifications = (
            db.query(NotificationLog)
            .filter(NotificationLog.event_id.in_(event_ids))
            .delete(synchronize_session=False)
        )
    deleted_events = (
        db.query(Event).filter(Event.pipeline_item_id.in_(found)).delete(synchronize_session=False)
    )
    db.query(KeyDate).filter(KeyDate.pipeline_item_id.in_(found)).delete(synchronize_session=False)
    db.query(JoinKey).filter(JoinKey.pipeline_item_id.in_(found)).delete(synchronize_session=False)
    db.query(PipelineItem).filter(PipelineItem.resolved_change_id.in_(found)).update(
        {PipelineItem.resolved_change_id: None}, synchronize_session=False
    )
    deleted_items = (
        db.query(PipelineItem).filter(PipelineItem.id.in_(found)).delete(synchronize_session=False)
    )
    return {
        "purged_items": found,
        "deleted_events": deleted_events,
        "deleted_notifications": deleted_notifications,
        "deleted_item_rows": deleted_items,
    }


# Known-bad rows produced by since-fixed parser defects: (source_system, title LIKE).
# Purged at boot so live deployments heal on deploy without a manual endpoint call;
# idempotent — once the rows are gone each pattern matches nothing.
KNOWN_BAD_ITEMS: list[tuple[str, str]] = [
    # NERC titles fabricated from webinar copy on the listing page (v0.5.5 parser fix);
    # the projects themselves were also non-CIP and out of scope
    ("nerc", "%Breakout Session%"),
    ("nerc", "%: and Project%"),
    # BIS export-controls rule admitted by the scope gate via an "AI" mention in the
    # abstract — export policy, not a security/privacy obligation (operator-reviewed)
    ("federal_register", "%United Arab Emirates Under the Export Administration%"),
]


def purge_known_bad(db: Session) -> int:
    ids: set[int] = set()
    for source, pattern in KNOWN_BAD_ITEMS:
        ids.update(
            i
            for (i,) in db.query(PipelineItem.id)
            .filter(PipelineItem.source_system == source, PipelineItem.title.like(pattern))
            .all()
        )
    if ids:
        purge_items(db, sorted(ids))
    return len(ids)
