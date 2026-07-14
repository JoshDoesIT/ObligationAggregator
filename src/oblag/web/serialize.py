from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from oblag.core.reducer import current_dates
from oblag.db.models import Event, KeyDate, PipelineItem, Snapshot


def snapshot_ref(session: Session, snapshot_id: int | None) -> dict[str, Any] | None:
    if snapshot_id is None:
        return None
    snap = session.get(Snapshot, snapshot_id)
    if snap is None:
        return None
    return {
        "sha256": snap.sha256,
        "source_url": snap.source_url,
        "fetched_at": snap.fetched_at.isoformat(),
        "adapter": snap.adapter,
        "attestation_ref": snap.attestation_ref,
    }


def key_date_to_dict(session: Session, kd: KeyDate, *, provenance: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": kd.id,
        "date_type": kd.date_type.value,
        "label": kd.label,
        "value": kd.value.isoformat(),
        "confidence": kd.confidence.value,
        "asserted_at": kd.asserted_at.isoformat() if kd.asserted_at else None,
        "supersedes_id": kd.supersedes_id,
    }
    if provenance:
        d["snapshot"] = snapshot_ref(session, kd.source_snapshot_id)
    return d


# Change-severity classification (research doc 2, feature 3): what kind of attention
# an event deserves. Derived, not stored — the event stream stays the source of truth.
_SEVERITY = {
    "item_created": "new_obligation",
    "state_changed": "substantive",
    "date_changed": "substantive",
    "item_resolved": "substantive",
    "content_changed": "editorial",
    "anomaly": "operational",
}


def event_severity(ev: Event) -> str:
    return _SEVERITY.get(ev.type.value, "substantive")


def event_to_dict(session: Session, ev: Event, *, provenance: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": ev.id,
        "item_id": ev.pipeline_item_id,
        "type": ev.type.value,
        "severity": event_severity(ev),
        "payload": ev.payload,
        "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
    }
    if provenance:
        d["snapshot"] = snapshot_ref(session, ev.snapshot_id)
    return d


def item_to_dict(session: Session, item: PipelineItem, *, detail: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": item.id,
        "source_system": item.source_system,
        "jurisdiction": item.jurisdiction,
        "title": item.title,
        "state": item.state.value,
        "track": item.track,
        "native_status": item.native_status,
        "url": item.url,
        "obligation": item.obligation.slug if item.obligation else None,
        "resolved_change_id": item.resolved_change_id,
        "first_seen_at": item.first_seen_at.isoformat() if item.first_seen_at else None,
        "last_seen_at": item.last_seen_at.isoformat() if item.last_seen_at else None,
        "join_keys": [{"type": k.type, "value": k.value} for k in item.join_keys],
        "current_dates": [
            key_date_to_dict(session, kd, provenance=detail)
            for kd in current_dates(session, item.id).values()
        ],
    }
    if detail:
        d["abstract"] = item.abstract
        d["native_meta"] = item.native_meta
        d["date_history"] = [
            key_date_to_dict(session, kd, provenance=True)
            for kd in sorted(item.key_dates, key=lambda k: k.id)
        ]
        d["events"] = [
            event_to_dict(session, ev, provenance=True)
            for ev in sorted(item.events, key=lambda e: e.id)
        ]
    return d
