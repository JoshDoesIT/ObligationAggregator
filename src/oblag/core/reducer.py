from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import or_, tuple_
from sqlalchemy.orm import Session

from oblag.adapters.base import NormalizedItem
from oblag.core.statemap import CurrentDateMap, compute_state
from oblag.core.transitions import Verdict, classify_transition
from oblag.db.models import (
    Event,
    EventType,
    ItemState,
    JoinKey,
    KeyDate,
    Obligation,
    PipelineItem,
)


def _resolve_obligation(session: Session, slug: str | None) -> int | None:
    if not slug:
        return None
    row = session.query(Obligation.id).filter_by(slug=slug).one_or_none()
    return row[0] if row else None


@dataclass
class ReduceResult:
    item: PipelineItem
    created: bool
    events: list[Event] = field(default_factory=list)


def _emit(
    session: Session,
    item: PipelineItem | None,
    type_: EventType,
    payload: dict,
    snapshot_id: int | None,
) -> Event:
    ev = Event(
        pipeline_item_id=item.id if item else None,
        type=type_,
        payload=payload,
        snapshot_id=snapshot_id,
    )
    session.add(ev)
    session.flush()
    return ev


def current_dates(session: Session, item_id: int) -> dict[tuple, KeyDate]:
    """Resolve supersession chains: for each (date_type, label) return the live KeyDate
    (the row no other row supersedes; ties broken by latest asserted_at/id).

    A chain ending in a retraction means the source withdrew the date — no current
    value exists for that (date_type, label)."""
    rows = session.query(KeyDate).filter_by(pipeline_item_id=item_id).all()
    superseded_ids = {r.supersedes_id for r in rows if r.supersedes_id is not None}
    live: dict[tuple, KeyDate] = {}
    for row in rows:
        if row.id in superseded_ids:
            continue
        key = (row.date_type, row.label)
        prev = live.get(key)
        if prev is None or row.id > prev.id:  # id is monotonic; avoids naive/aware tz compares
            live[key] = row
    return {k: v for k, v in live.items() if not v.retracted}


def _date_values(live: dict[tuple, KeyDate]) -> CurrentDateMap:
    return {k: v.value for k, v in live.items()}


def _find_item(session: Session, ni: NormalizedItem) -> tuple[PipelineItem | None, str | None]:
    """Resolve identity by join keys within the same lifecycle track (spec 03 step 1).

    Returns (item, anomaly_note). anomaly_note is set when the keys ambiguously
    matched multiple items."""
    keys = ni.all_join_keys
    rows = session.query(JoinKey).filter(or_(tuple_(JoinKey.type, JoinKey.value).in_(keys))).all()
    if not rows:
        return None, None
    items = {r.item.id: r.item for r in rows if r.item.track == ni.track}
    if items and not ni.supplementary:
        # Identity guard: a candidate whose own external-type keys all differ from this
        # document's external key is a DIFFERENT document that merely shares an umbrella
        # join key (agency-wide RIN, fisheries docket). Merging would splice two
        # rulemakings into one item (observed live: distinct airworthiness directives
        # via FAA RIN 2120-AA64). Only supplementary docs may cross that line.
        ext_type, ext_value = ni.external_key
        ext_rows = (
            session.query(JoinKey)
            .filter(JoinKey.pipeline_item_id.in_(list(items)), JoinKey.type == ext_type)
            .all()
        )
        typed: dict[int, set[str]] = {}
        for r in ext_rows:
            typed.setdefault(r.pipeline_item_id, set()).add(r.value)
        items = {
            iid: it for iid, it in items.items() if iid not in typed or ext_value in typed[iid]
        }
    if not items:
        return None, None
    if len(items) == 1:
        return next(iter(items.values())), None
    # ambiguous: prefer exact external_key match, else most recently seen; flag anomaly
    ext_type, ext_value = ni.external_key
    for r in rows:
        if r.type == ext_type and r.value == ext_value and r.item.id in items:
            chosen = r.item
            break
    else:
        chosen = max(items.values(), key=lambda i: i.id)
    note = (
        f"join keys {keys!r} matched {len(items)} items in track {ni.track!r}; "
        f"chose item {chosen.id}"
    )
    return chosen, note


def _apply_state(
    session: Session,
    item: PipelineItem,
    target: ItemState | None,
    snapshot_id: int | None,
    events: list[Event],
) -> None:
    if target is None:
        events.append(
            _emit(
                session,
                item,
                EventType.anomaly,
                {"kind": "unknown_native_status", "detail": item.native_status or ""},
                snapshot_id,
            )
        )
        return
    verdict = classify_transition(item.state, target)
    if verdict is Verdict.noop:
        return
    if verdict is Verdict.anomaly:
        events.append(
            _emit(
                session,
                item,
                EventType.anomaly,
                {
                    "kind": "illegal_transition",
                    "detail": f"{item.state.value} → {target.value} rejected; state kept",
                },
                snapshot_id,
            )
        )
        return
    old = item.state
    item.state = target
    events.append(
        _emit(
            session,
            item,
            EventType.state_changed,
            {"from": old.value, "to": target.value},
            snapshot_id,
        )
    )


def reduce_item(
    session: Session,
    ni: NormalizedItem,
    snapshot_id: int | None = None,
    today: date | None = None,
) -> ReduceResult:
    """Spec 03 reducer: identity → dates (append-only) → state → content → keys."""
    today = today or datetime.now(UTC).date()
    item, ambiguity_note = _find_item(session, ni)
    events: list[Event] = []

    if item is None:
        target = compute_state(
            ni.source_system, ni.native_status, ni.native_meta, _incoming_dates(ni), today
        )
        item = PipelineItem(
            source_system=ni.source_system,
            jurisdiction=ni.jurisdiction,
            title=ni.title,
            abstract=ni.abstract,
            url=ni.url,
            state=target or ItemState.proposed,
            native_status=ni.native_status,
            native_meta=dict(ni.native_meta),
            track=ni.track,
            content_fingerprint=ni.content_fingerprint,
            obligation_id=_resolve_obligation(session, ni.obligation_slug),
        )
        session.add(item)
        session.flush()
        for ktype, kvalue in ni.all_join_keys:
            session.add(JoinKey(pipeline_item_id=item.id, type=ktype, value=kvalue))
        for nd in ni.dates:
            session.add(
                KeyDate(
                    pipeline_item_id=item.id,
                    date_type=nd.date_type,
                    label=nd.label,
                    value=nd.value,
                    confidence=nd.confidence,
                    source_snapshot_id=snapshot_id,
                )
            )
        session.flush()
        events.append(
            _emit(
                session,
                item,
                EventType.item_created,
                {"title": ni.title, "source": ni.source_system, "track": ni.track},
                snapshot_id,
            )
        )
        events.append(
            _emit(
                session,
                item,
                EventType.state_changed,
                {"from": None, "to": item.state.value},
                snapshot_id,
            )
        )
        if target is None:
            events.append(
                _emit(
                    session,
                    item,
                    EventType.anomaly,
                    {"kind": "unknown_native_status", "detail": ni.native_status},
                    snapshot_id,
                )
            )
        _note_item_anomalies(session, item, ni, snapshot_id, events)
        return ReduceResult(item=item, created=True, events=events)

    if ambiguity_note:
        events.append(
            _emit(
                session,
                item,
                EventType.anomaly,
                {"kind": "ambiguous_join_keys", "detail": ambiguity_note},
                snapshot_id,
            )
        )

    # 2. dates: append-only supersession
    live = current_dates(session, item.id)
    for nd in ni.dates:
        key = (nd.date_type, nd.label)
        cur = live.get(key)
        if cur is not None and cur.value == nd.value:
            continue
        new_row = KeyDate(
            pipeline_item_id=item.id,
            date_type=nd.date_type,
            label=nd.label,
            value=nd.value,
            confidence=nd.confidence,
            source_snapshot_id=snapshot_id,
            supersedes_id=cur.id if cur else None,
        )
        session.add(new_row)
        session.flush()
        live[key] = new_row
        events.append(
            _emit(
                session,
                item,
                EventType.date_changed,
                {
                    "date_type": nd.date_type.value,
                    "label": nd.label,
                    "from": cur.value.isoformat() if cur else None,
                    "to": nd.value.isoformat(),
                    "confidence": nd.confidence.value,
                    "superseded_key_date_id": cur.id if cur else None,
                },
                snapshot_id,
            )
        )

    # 2b. retractions: the source explicitly no longer states these date types.
    # Append-only like everything else — a retracted row supersedes the value and
    # keeps it in the audit trail. Idempotent: an already-retracted chain has no
    # live entry, so repeated "no due date" signals are silent.
    for retract_type in ni.retract_dates:
        for key, cur in list(live.items()):
            if key[0] is not retract_type:
                continue
            retraction = KeyDate(
                pipeline_item_id=item.id,
                date_type=cur.date_type,
                label=cur.label,
                value=cur.value,
                confidence=cur.confidence,
                retracted=True,
                source_snapshot_id=snapshot_id,
                supersedes_id=cur.id,
            )
            session.add(retraction)
            session.flush()
            del live[key]
            events.append(
                _emit(
                    session,
                    item,
                    EventType.date_changed,
                    {
                        "date_type": cur.date_type.value,
                        "label": cur.label,
                        "from": cur.value.isoformat(),
                        "to": None,
                        "retracted": True,
                        "superseded_key_date_id": cur.id,
                    },
                    snapshot_id,
                )
            )

    # 5. merge new join keys (done before state so statemap sees a settled item)
    existing_keys = {(k.type, k.value) for k in item.join_keys}
    for ktype, kvalue in ni.all_join_keys:
        if (ktype, kvalue) in existing_keys:
            continue
        clash = (
            session.query(JoinKey)
            .filter_by(type=ktype, value=kvalue)
            .join(PipelineItem, JoinKey.pipeline_item_id == PipelineItem.id)
            .filter(PipelineItem.track == item.track, PipelineItem.id != item.id)
            .first()
        )
        if clash is not None:
            events.append(
                _emit(
                    session,
                    item,
                    EventType.anomaly,
                    {
                        "kind": "join_key_conflict",
                        "detail": f"({ktype},{kvalue}) already bound to item "
                        f"{clash.pipeline_item_id}; not merged",
                    },
                    snapshot_id,
                )
            )
            continue
        session.add(JoinKey(pipeline_item_id=item.id, type=ktype, value=kvalue))
    session.flush()
    session.refresh(item)

    # 3. state from statemap over merged current dates
    if ni.native_meta:
        item.native_meta = {**(item.native_meta or {}), **ni.native_meta}
    item.native_status = ni.native_status
    target = compute_state(
        ni.source_system, ni.native_status, item.native_meta or {}, _date_values(live), today
    )
    if item.state is ItemState.superseded and item.resolved_change_id is not None:
        # linker-resolved items legitimately reappear in fetch windows; not an anomaly
        pass
    else:
        _apply_state(session, item, target, snapshot_id, events)

    # 4. content change
    if item.content_fingerprint != ni.content_fingerprint:
        events.append(
            _emit(
                session,
                item,
                EventType.content_changed,
                {"from": item.content_fingerprint, "to": ni.content_fingerprint},
                snapshot_id,
            )
        )
        item.content_fingerprint = ni.content_fingerprint
        item.title = ni.title
        item.abstract = ni.abstract
        item.url = ni.url or item.url

    if item.obligation_id is None and ni.obligation_slug:
        item.obligation_id = _resolve_obligation(session, ni.obligation_slug)
    item.last_seen_at = datetime.now(UTC)
    _note_item_anomalies(session, item, ni, snapshot_id, events)
    session.flush()
    return ReduceResult(item=item, created=False, events=events)


def _incoming_dates(ni: NormalizedItem) -> CurrentDateMap:
    return {(d.date_type, d.label): d.value for d in ni.dates}


def _note_item_anomalies(
    session: Session,
    item: PipelineItem,
    ni: NormalizedItem,
    snapshot_id: int | None,
    events: list[Event],
) -> None:
    for note in ni.anomalies:
        events.append(
            _emit(
                session,
                item,
                EventType.anomaly,
                {"kind": "adapter_parse", "detail": note},
                snapshot_id,
            )
        )


_ACTIVE_STATES = (
    ItemState.proposed,
    ItemState.comment_open,
    ItemState.comment_closed,
    ItemState.final_pending_effective,
    ItemState.stalled,
)


def tick(session: Session, today: date | None = None) -> list[Event]:
    """Daily re-evaluation: time-based transitions from stored dates, no fetch (spec 03)."""
    today = today or datetime.now(UTC).date()
    events: list[Event] = []
    items = session.query(PipelineItem).filter(PipelineItem.state.in_(_ACTIVE_STATES)).all()
    for item in items:
        live = current_dates(session, item.id)
        target = compute_state(
            item.source_system,
            item.native_status or "",
            item.native_meta or {},
            _date_values(live),
            today,
        )
        if target is None or target == item.state:
            continue
        _apply_state(session, item, target, None, events)
    session.flush()
    return events
