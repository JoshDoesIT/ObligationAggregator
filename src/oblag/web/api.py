from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from oblag.db.models import (
    AdapterHealth,
    Event,
    EventType,
    ItemState,
    JoinKey,
    KeyDate,
    Obligation,
    PipelineItem,
)
from oblag.web.deps import get_db
from oblag.web.serialize import event_to_dict, item_to_dict

router = APIRouter(prefix="/api/v1")


def _apply_item_filters(
    query,
    *,
    state: list[str] | None,
    source: list[str] | None,
    jurisdiction: list[str] | None,
    track: str | None,
    q: str | None,
    obligation: str | None = None,
):
    if state:
        query = query.filter(PipelineItem.state.in_([ItemState(s) for s in state]))
    if obligation:
        query = query.join(Obligation, PipelineItem.obligation_id == Obligation.id).filter(
            Obligation.slug == obligation
        )
    if source:
        query = query.filter(PipelineItem.source_system.in_(source))
    if jurisdiction:
        query = query.filter(PipelineItem.jurisdiction.in_(jurisdiction))
    if track:
        query = query.filter(PipelineItem.track == track)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(PipelineItem.title.ilike(like), PipelineItem.abstract.ilike(like)))
    return query


@router.get("/items")
def list_items(
    db: Session = Depends(get_db),
    state: list[str] | None = Query(None),
    source: list[str] | None = Query(None),
    jurisdiction: list[str] | None = Query(None),
    track: str | None = None,
    q: str | None = None,
    obligation: str | None = None,
    limit: int = Query(50, le=500),
    offset: int = 0,
    # The HTML pages pass the org's obligation scope here. Hidden from the schema and
    # defaulted to None so the JSON API stays unscoped: narrowing a programmatic
    # client's results behind its back would be a trap.
    scope: Annotated[list[str] | None, Query(include_in_schema=False)] = None,
):
    try:
        query = _apply_item_filters(
            db.query(PipelineItem),
            state=state,
            source=source,
            jurisdiction=jurisdiction,
            track=track,
            q=q,
            obligation=obligation,
        )
        if scope:
            query = query.join(
                Obligation, PipelineItem.obligation_id == Obligation.id, isouter=False
            ).filter(Obligation.slug.in_(scope))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    total = query.count()
    from sqlalchemy import func
    from sqlalchemy.orm import joinedload, selectinload

    from oblag.web.serialize import items_to_dicts

    # Newest by the SOURCE's clock, falling back to when we first saw it. Ordering by
    # last_seen_at ranked by our own polling instead: every item an adapter re-observed
    # jumped to the top, and after a rebuild the whole feed shares one timestamp and
    # comes back in arbitrary order.
    items = (
        query.options(selectinload(PipelineItem.join_keys), joinedload(PipelineItem.obligation))
        .order_by(
            # anything we can date reads in true chronological order; what we cannot
            # date follows, rather than jumping the queue on our ingestion clock
            PipelineItem.published_at.is_(None),
            func.coalesce(PipelineItem.published_at, PipelineItem.first_seen_at).desc(),
            PipelineItem.id.desc(),
        )
        .limit(limit)
        .offset(offset)
        .all()
    )
    return {"total": total, "items": items_to_dicts(db, items)}


@router.get("/items/{item_id}")
def get_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(PipelineItem, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    return item_to_dict(db, item, detail=True)


@router.get("/events")
def list_events(
    db: Session = Depends(get_db),
    type: list[str] | None = Query(None),
    item_id: int | None = None,
    limit: int = Query(100, le=1000),
    offset: int = 0,
):
    query = db.query(Event)
    if type:
        try:
            query = query.filter(Event.type.in_([EventType(t) for t in type]))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    if item_id:
        query = query.filter(Event.pipeline_item_id == item_id)
    total = query.count()
    events = query.order_by(Event.id.desc()).limit(limit).offset(offset).all()
    return {"total": total, "events": [event_to_dict(db, e, provenance=True) for e in events]}


DEADLINE_TYPES = frozenset(
    {
        "comment_close",
        "effective",
        "phased_compliance",
        "application",
        "entry_into_force",
        "transition_deadline",
        "projected_final",
    }
)


@router.get("/deadlines")
def upcoming_deadlines(
    db: Session = Depends(get_db),
    date_type: list[str] | None = Query(None),
    within_days: int = Query(90, le=3650),
    scope: Annotated[list[str] | None, Query(include_in_schema=False)] = None,
):
    """Countdown view: current (non-superseded) future *deadlines*, soonest first.
    proposal/adoption dates are milestones, not deadlines — excluded by default."""
    today = date.today()
    horizon = today + timedelta(days=within_days)
    rows = (
        db.query(KeyDate)
        .filter(
            KeyDate.value >= today,
            KeyDate.value <= horizon,
            KeyDate.retracted.is_(False),  # a retraction row is not a deadline
        )
        .order_by(KeyDate.value)
        .all()
    )
    superseded = {
        r.supersedes_id
        for r in db.query(KeyDate.supersedes_id).filter(KeyDate.supersedes_id.isnot(None))
    }
    wanted = set(date_type) if date_type else DEADLINE_TYPES
    # Resolve every owning item in one query — this ran per deadline row, so a busy
    # horizon cost one round trip per date (and the landing page now calls this too).
    kept = [kd for kd in rows if kd.id not in superseded and kd.date_type.value in wanted]
    items_by_id = {
        it.id: it
        for it in db.query(PipelineItem).filter(
            PipelineItem.id.in_({kd.pipeline_item_id for kd in kept})
        )
    } or {}
    in_scope: set[int] | None = None
    if scope:
        in_scope = {oid for (oid,) in db.query(Obligation.id).filter(Obligation.slug.in_(scope))}
    out = []
    for kd in kept:
        item = items_by_id.get(kd.pipeline_item_id)
        if item is None or item.state.value in ("withdrawn", "superseded"):
            continue
        if in_scope is not None and item.obligation_id not in in_scope:
            continue
        out.append(
            {
                "item_id": item.id,
                "title": item.title,
                "state": item.state.value,
                "date_type": kd.date_type.value,
                "label": kd.label,
                "value": kd.value.isoformat(),
                "confidence": kd.confidence.value,
                "days_until": (kd.value - today).days,
            }
        )
    return {"deadlines": out}


@router.get("/health")
def adapter_health(db: Session = Depends(get_db)):
    rows = db.query(AdapterHealth).all()
    return {
        "adapters": [
            {
                "adapter": r.adapter,
                "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
                "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
                "consecutive_failures": r.consecutive_failures,
                "last_error": r.last_error,
                "items_seen_last_run": r.items_seen_last_run,
            }
            for r in rows
        ]
    }


@router.get("/obligations")
def list_obligations(db: Session = Depends(get_db)):
    rows = db.query(Obligation).order_by(Obligation.slug).all()
    return {
        "obligations": [
            {
                "slug": o.slug,
                "name": o.name,
                "issuing_body": o.issuing_body,
                "jurisdiction": o.jurisdiction,
                "current_version": o.effective_version,
                "copyright_status": o.copyright_status.value,
                "display_policy": o.display_policy.value,
                "canonical_url": o.canonical_url,
            }
            for o in rows
        ]
    }


@router.get("/export/oscal")
def export_oscal(obligation: str | None = None, db: Session = Depends(get_db)):
    from oblag.oscal import export_catalog

    try:
        return export_catalog(db, obligation)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@router.get("/items/by-key/{key_type}/{key_value:path}")
def get_item_by_join_key(key_type: str, key_value: str, db: Session = Depends(get_db)):
    rows = db.query(JoinKey).filter_by(type=key_type, value=key_value).all()
    if not rows:
        raise HTTPException(404, "no item with that join key")
    return {"items": [item_to_dict(db, r.item) for r in rows]}
