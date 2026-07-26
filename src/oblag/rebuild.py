"""Rebuild the pipeline from the sources: re-read the record, then drop what the
sources no longer list.

Why this exists: adapter fixes only reach rows the source still lists. A parser bug
that mangled a title, or a column added after ingestion (`published_at`, v0.14.0),
leaves older rows stale forever because nothing re-observes them. Rebuilding is the
honest repair — refetch the record rather than hand-edit the database.

Deliberately REFRESH-THEN-PRUNE rather than wipe-then-refill. A wipe assumes every
source will answer; the first live trial proved otherwise (iso.org served 403 to the
catalogue adapter, which would have deleted those items with nothing to restore them
from). Re-ingesting first means a source we cannot reach simply keeps the rows it gave
us last time, and only a source that answered can retire its own stale rows.

Never pruned, whatever the sources say:
  * items carrying a curated date (a human asserted it; no feed will bring it back)
  * anything from a source that errored, was skipped, or was not part of this run
  * tenancy, the obligation catalog, and the snapshot/attestation store
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from typing import Any

from sqlalchemy.orm import Session

from oblag.db.models import Confidence, DateType, JoinKey, KeyDate, PipelineItem

log = logging.getLogger(__name__)

# Sources that accept a date window, so a rebuild can reach back years. Everything
# else serves "whatever the feed carries right now" — a rebuild refreshes those but
# cannot invent history that the publisher no longer lists.
WINDOWED_ADAPTERS = ("federal_register", "regulations_gov", "cellar")
# 90-day slices: the Federal Register caps results per query, and a slice keeps each
# HTTP page small enough that one slow window can't blow a serverless invocation.
# Measured live: 730 days of security/privacy rules = 135 items in 35s at this size.
WINDOW_DAYS = 90


@dataclass
class RebuildReport:
    purged: dict[str, Any] = field(default_factory=dict)
    curated_saved: int = 0
    curated_replayed: int = 0
    curated_orphaned: int = 0
    ran: list[dict[str, Any]] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "purged": self.purged,
            "curated": {
                "saved": self.curated_saved,
                "replayed": self.curated_replayed,
                "orphaned": self.curated_orphaned,
            },
            "ran": self.ran,
            "deferred": self.deferred,
            "errors": self.errors,
        }


def export_curated(db: Session) -> list[dict[str, Any]]:
    """Human-entered date assertions, keyed by their item's join keys.

    A curated row is one with no source snapshot: the reducer always attaches the
    snapshot it read a date from, so `source_snapshot_id IS NULL` is exactly the set a
    person (or the milestone seeder) asserted by hand. Keyed by join keys rather than
    item id because ids do not survive the wipe."""
    rows = (
        db.query(KeyDate, PipelineItem)
        .join(PipelineItem, KeyDate.pipeline_item_id == PipelineItem.id)
        .filter(KeyDate.source_snapshot_id.is_(None), KeyDate.retracted.is_(False))
        .all()
    )
    if not rows:
        return []
    keys_by_item: dict[int, list[tuple[str, str]]] = {}
    for jk in db.query(JoinKey).filter(JoinKey.pipeline_item_id.in_([i.id for _, i in rows])).all():
        keys_by_item.setdefault(jk.pipeline_item_id, []).append((jk.type, jk.value))
    saved = []
    for kd, item in rows:
        keys = keys_by_item.get(item.id, [])
        if not keys:
            continue  # nothing to match it back to after the wipe
        saved.append(
            {
                "join_keys": keys,
                "date_type": kd.date_type.value,
                "label": kd.label,
                "value": kd.value.isoformat(),
                "confidence": kd.confidence.value,
                "title": item.title,
            }
        )
    return saved


def replay_curated(db: Session, saved: list[dict[str, Any]]) -> tuple[int, int]:
    """Re-apply saved assertions to whichever rebuilt item now carries their join keys.
    Returns (replayed, orphaned) — orphaned means the source no longer lists that item,
    so there is nothing to attach the date to."""
    from oblag.core.assertions import assert_date

    replayed = orphaned = 0
    for row in saved:
        item_id = None
        for ktype, kvalue in row["join_keys"]:
            hit = db.query(JoinKey).filter_by(type=ktype, value=kvalue).first()
            if hit is not None:
                item_id = hit.pipeline_item_id
                break
        if item_id is None:
            orphaned += 1
            log.info("curated date orphaned by rebuild: %s", row.get("title"))
            continue
        assert_date(
            db,
            item_id,
            DateType(row["date_type"]),
            date.fromisoformat(row["value"]),
            Confidence(row["confidence"]),
            label=row["label"],
            note="restored by rebuild",
        )
        replayed += 1
    db.flush()
    return replayed, orphaned


# Sources that enumerate their whole corpus every run (a sitemap, or a date-windowed
# API asked for the full window). Only these can say "this item is gone" by omission.
# An RSS feed rolling an old post off the bottom means nothing of the sort, so pruning
# never applies to feed-shaped sources.
ENUMERATING_ADAPTERS = frozenset({"hitrust", "aicpa", *WINDOWED_ADAPTERS})


def prune_stale(db: Session, *, since: datetime, healthy: set[str]) -> dict[str, Any]:
    """Retire rows that an enumerating source stopped listing.

    `since` is the moment the rebuild started: the reducer stamps last_seen_at on every
    item it touches, so anything older than that was not in what the source just served.
    Items with a curated date are kept regardless — a person put that there."""
    from oblag.maintenance import purge_items

    sources = sorted(healthy & ENUMERATING_ADAPTERS)
    if not sources:
        return {"pruned": [], "kept_curated": 0, "sources": []}
    candidates = [
        i
        for (i,) in db.query(PipelineItem.id).filter(
            PipelineItem.source_system.in_(sources), PipelineItem.last_seen_at < since
        )
    ]
    if not candidates:
        return {"pruned": [], "kept_curated": 0, "sources": sources}
    annotated = {
        i
        for (i,) in db.query(KeyDate.pipeline_item_id).filter(
            KeyDate.pipeline_item_id.in_(candidates), KeyDate.source_snapshot_id.is_(None)
        )
    }
    doomed = [i for i in candidates if i not in annotated]
    result = purge_items(db, doomed) if doomed else {"purged_items": []}
    db.flush()
    return {
        "pruned": result["purged_items"],
        "kept_curated": len(annotated),
        "sources": sources,
    }


def backfill_plan(days: int, adapters: list[str] | None = None) -> list[tuple[str, tuple | None]]:
    """(adapter, window) pairs. Windowed sources get one entry per slice, oldest first
    so the feed fills chronologically; feed sources get a single windowless run."""
    from oblag.adapters import available_adapters

    names = adapters or list(available_adapters())
    today = date.today()
    plan: list[tuple[str, tuple | None]] = []
    for name in names:
        if name not in WINDOWED_ADAPTERS:
            plan.append((name, None))
            continue
        cursor = today - timedelta(days=days)
        while cursor <= today:
            stop = min(cursor + timedelta(days=WINDOW_DAYS), today)
            plan.append((name, (cursor, stop)))
            cursor = stop + timedelta(days=1)
    return plan


def run_plan(
    db: Session,
    plan: list[tuple[str, tuple | None]],
    *,
    budget_s: float | None = None,
    report: RebuildReport | None = None,
) -> RebuildReport:
    """Execute a plan, stopping cleanly when the time budget runs out. One failing
    source never aborts the rest (spec 02): it lands in `errors` and the run goes on."""
    from oblag.adapters import available_adapters, get_adapter
    from oblag.core.runner import run_adapter

    rep = report or RebuildReport()
    start = monotonic()
    for name, window in plan:
        if budget_s is not None and monotonic() - start > budget_s:
            if name not in rep.deferred:
                rep.deferred.append(name)
            continue
        if name not in available_adapters() or not get_adapter(name).enabled():
            continue
        try:
            stats = run_adapter(db, name, window=window)
            rep.ran.append(
                {
                    "adapter": name,
                    "window": [w.isoformat() for w in window] if window else None,
                    "items": stats.items,
                    "created": stats.created,
                    "errors": len(stats.errors),
                }
            )
        except Exception as exc:  # noqa: BLE001 — a dead source must not stop a rebuild
            log.exception("rebuild run failed for %s", name)
            rep.errors.append(f"{name}: {str(exc)[:200]}")
    return rep


CATCHUP_KEY = "backfill_catchup"


def catchup(db: Session, *, days: int = 730, budget_s: float | None = None) -> dict[str, Any]:
    """Work through the historical backfill a slice at a time, resuming across runs.

    The daily cron calls this, and Vercel signs its own cron invocations, so a
    deployment fills in its own history with nobody handling a secret. Progress lives
    in kv_meta, so an invocation that runs out of time picks up where it stopped and
    the whole thing happens at most once per `days` setting.

    Backfill only: it never prunes. Retiring rows needs the whole plan to have run in
    one pass, which is exactly what a resumable job cannot promise."""
    import json
    from time import monotonic

    from oblag.adapters import available_adapters, get_adapter
    from oblag.core.runner import run_adapter
    from oblag.db.models import KVMeta

    row = db.get(KVMeta, CATCHUP_KEY)
    state = json.loads(row.value) if row else {}
    if state.get("done_for_days") == days:
        return {"status": "already_done", "days": days}

    plan = backfill_plan(days)
    # a changed `days` restarts the plan; otherwise resume where the last run stopped
    index = int(state.get("index", 0)) if state.get("days") == days else 0
    started = monotonic()
    ran = 0
    stepped = 0
    errors: list[str] = []
    while index < len(plan):
        # always take at least one slice per invocation: checking the budget first
        # meant a run that arrived with none left made no progress at all, and the
        # queue would sit there forever waiting for a quiet day that never comes
        if stepped and budget_s is not None and monotonic() - started > budget_s:
            break
        stepped += 1
        name, window = plan[index]
        index += 1
        if name not in available_adapters() or not get_adapter(name).enabled():
            continue
        try:
            run_adapter(db, name, window=window)
            ran += 1
        except Exception as exc:  # noqa: BLE001 — a dead source must not stall the queue
            log.exception("catchup run failed for %s", name)
            errors.append(f"{name}: {str(exc)[:120]}")

    finished = index >= len(plan)
    new_state: dict[str, Any] = {"days": days, "index": index}
    if finished:
        new_state["done_for_days"] = days
    payload = json.dumps(new_state)
    if row is None:
        db.add(KVMeta(key=CATCHUP_KEY, value=payload))
    else:
        row.value = payload
        row.updated_at = utcnow()
    db.flush()
    return {
        "status": "done" if finished else "in_progress",
        "days": days,
        "step": index,
        "of": len(plan),
        "ran": ran,
        "errors": errors,
    }


def rebuild(
    db: Session,
    *,
    days: int = 730,
    adapters: list[str] | None = None,
    budget_s: float | None = None,
    prune: bool = True,
) -> RebuildReport:
    """Re-read every source over `days`, then retire what the enumerating ones dropped.

    Safe to re-run: the reducer matches on join keys, so a second pass updates rather
    than duplicates. Set prune=False to add history without retiring anything."""
    from oblag.catalog import seed_obligations
    from oblag.milestones import seed_milestones

    rep = RebuildReport()
    started = utcnow()

    seed_obligations(db)
    db.commit()

    run_plan(db, backfill_plan(days, adapters), budget_s=budget_s, report=rep)
    db.commit()

    # milestone timelines are curated content with no feed behind them
    seed_milestones(db)
    db.commit()

    if prune and not rep.deferred:
        # a partial run has not heard from every source yet, so it must not retire
        # anything: the rows it would drop may simply be waiting on a deferred window
        failed = {e.split(":", 1)[0] for e in rep.errors}
        healthy = {r["adapter"] for r in rep.ran} - failed
        rep.purged = prune_stale(db, since=started, healthy=healthy)
        db.commit()
    log.info("rebuild complete: %s", rep.as_dict())
    return rep


def utcnow() -> datetime:
    return datetime.now(UTC)
