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
    # BIS export-controls rule admitted by the scope gate via an "AI" mention in the
    # abstract — export policy, not a security/privacy obligation (operator-reviewed)
    ("federal_register", "%United Arab Emirates Under the Export Administration%"),
]


def complete_concluded_consultations(db: Session) -> int:
    """Flip comment_closed consultations with a recorded (past) `adopted` date to
    effective. The statemap does this on re-reduce, but re-reduction only happens when
    the source feed still lists the item — old initiatives may never re-appear, so a
    curated adoption could otherwise leave the state stale forever. Idempotent."""
    from datetime import date as _date

    from oblag.core.reducer import current_dates
    from oblag.db.models import DateType, Event, EventType, ItemState

    flipped = 0
    candidates = db.query(PipelineItem).filter(PipelineItem.state == ItemState.comment_closed)
    for item in candidates.all():
        adopted = next(
            (
                kd.value
                for (dt, _label), kd in current_dates(db, item.id).items()
                if dt is DateType.adopted
            ),
            None,
        )
        if adopted is None or adopted > _date.today():
            continue
        item.state = ItemState.effective
        db.add(
            Event(
                pipeline_item_id=item.id,
                type=EventType.state_changed,
                payload={"from": ItemState.comment_closed.value, "to": ItemState.effective.value},
            )
        )
        flipped += 1
    db.flush()
    return flipped


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


def dedupe_split_identities(db: Session) -> dict[str, list[int]]:
    """Retire the older half of items an adapter split by changing how it tracks a source.

    iso_catalog moved from track "default" to "final" in v0.19.0, and the reducer's
    same-track guard refused to match across the change, so every base ISO standard came
    back a second time beside its old row. The reducer no longer splits this way, but
    rows already split need clearing.

    This is a DESTRUCTIVE repair, so it is scoped tightly to that exact shape: same
    source, same join key, same title, and more than one track represented. All four
    together are what "one document our own modelling split in two" looks like.

    Grouping on the join key alone is not enough, and shipping it that way deleted 26
    live Federal Register items. Distinct rulemakings legitimately share umbrella keys
    (every airworthiness directive hangs off FAA RIN 2120-AA64) — the same hole the
    reducer's identity guard exists to close. Two documents that merely share a docket
    differ in title AND sit on one track, so either extra condition would have held.

    The survivor is the most recently seen row, since that is the one the current
    adapter is maintaining. A row carrying a curated date is never retired."""
    from collections import defaultdict

    groups: dict[tuple[str, str, str, str], list[PipelineItem]] = defaultdict(list)
    rows = (
        db.query(JoinKey.type, JoinKey.value, PipelineItem)
        .join(PipelineItem, JoinKey.pipeline_item_id == PipelineItem.id)
        .all()
    )
    for ktype, kvalue, item in rows:
        groups[(item.source_system, ktype, kvalue, item.title)].append(item)

    doomed: list[int] = []
    kept: list[int] = []
    for members in groups.values():
        unique = {i.id: i for i in members}
        if len(unique) < 2 or len({i.track for i in unique.values()}) < 2:
            continue
        ordered = sorted(unique.values(), key=lambda i: (i.last_seen_at or i.first_seen_at, i.id))
        survivor = ordered[-1]
        kept.append(survivor.id)
        doomed.extend(i.id for i in ordered[:-1])

    if doomed:
        annotated = {
            i
            for (i,) in db.query(KeyDate.pipeline_item_id).filter(
                KeyDate.pipeline_item_id.in_(doomed), KeyDate.source_snapshot_id.is_(None)
            )
        }
        doomed = [i for i in doomed if i not in annotated]
    if doomed:
        purge_items(db, sorted(doomed))
    return {"purged": sorted(doomed), "kept": sorted(kept)}


# Sources broad enough to need the relevance gate (oblag.scope). Everything else is a
# security/privacy publisher by definition and never consults it.
GATED_SOURCES = ("federal_register", "cellar", "have_your_say")


def rescope_items(db: Session) -> list[int]:
    """Retire ingested items the relevance gate would no longer admit.

    Tightening the gate only stops NEW noise; what is already in the feed stays until
    something clears it. Measured live before the v0.21.0 tightening: halibut fishery
    rules, HUD noise abatement, MARAD citizenship and a futures-trading RFC had all been
    admitted on a passing mention in their abstracts.

    Destructive, so it re-runs the gate the adapters use rather than inventing a rule,
    and it keeps anything with a reason to stay:
      * an item linked to a tracked obligation is relevant by definition whatever its
        wording — the CELEX corrigenda to DORA and NIS2 have bare-id titles;
      * a curated date means a person asserted something about it;
      * sources that never consult the gate are never judged by it."""
    from oblag.scope import in_scope

    candidates = (
        db.query(PipelineItem)
        .filter(
            PipelineItem.source_system.in_(GATED_SOURCES),
            PipelineItem.obligation_id.is_(None),
        )
        .all()
    )
    doomed = [i.id for i in candidates if not in_scope(i.title, i.abstract)]
    if doomed:
        annotated = {
            i
            for (i,) in db.query(KeyDate.pipeline_item_id).filter(
                KeyDate.pipeline_item_id.in_(doomed), KeyDate.source_snapshot_id.is_(None)
            )
        }
        doomed = [i for i in doomed if i not in annotated]
    if doomed:
        purge_items(db, sorted(doomed))
    return sorted(doomed)


def rescope_sitemap_items(db: Session) -> list[int]:
    """Re-apply the AICPA adapter's own URL filter to rows already stored.

    Same idea as rescope_items, different admission test: AICPA decides relevance from
    the sitemap URL rather than the scope vocabulary, so tightening that filter needs
    its own pass. Seven CPA professional-conduct drafts were live when "ethics" left
    the include list — loans, unpaid fees, tax services, section 529 plans."""
    from oblag.adapters.aicpa import _OFF_TOPIC_RE, _RELEVANT_RE

    rows = db.query(PipelineItem).filter(PipelineItem.source_system == "aicpa").all()
    doomed = [
        i.id
        for i in rows
        if i.url and (not _RELEVANT_RE.search(i.url) or _OFF_TOPIC_RE.search(i.url))
    ]
    if doomed:
        annotated = {
            i
            for (i,) in db.query(KeyDate.pipeline_item_id).filter(
                KeyDate.pipeline_item_id.in_(doomed), KeyDate.source_snapshot_id.is_(None)
            )
        }
        doomed = [i for i in doomed if i not in annotated]
    if doomed:
        purge_items(db, sorted(doomed))
    return sorted(doomed)


def purge_retired_sources(db: Session, keep: set[str]) -> list[int]:
    """Retire items from adapters this build no longer ships (NERC, v0.21.0).

    An item whose adapter is gone can never be re-observed, corrected or retired by its
    own source, so it would sit in the feed unchanged forever. `keep` is the live
    adapter registry; "curated" is always kept because a person put it there."""
    doomed = [
        i
        for (i,) in db.query(PipelineItem.id).filter(
            PipelineItem.source_system.notin_(sorted(keep | {"curated"}))
        )
    ]
    if doomed:
        purge_items(db, doomed)
    return sorted(doomed)


def rearm_backfill(db: Session) -> bool:
    """Clear the historical catch-up marker so the next daily cron re-reads the sources.

    Needed after a repair deleted rows it should not have: re-running the backfill
    restores anything a source still lists, and costs nothing when nothing was lost,
    because the reducer matches on join keys and a re-observed document updates its row
    rather than duplicating it. Returns whether a marker was actually cleared."""
    from oblag.db.models import KVMeta
    from oblag.rebuild import CATCHUP_KEY

    row = db.get(KVMeta, CATCHUP_KEY)
    if row is None:
        return False
    db.delete(row)
    return True
