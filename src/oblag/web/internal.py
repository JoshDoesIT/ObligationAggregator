"""Internal endpoints for scheduled ingestion on serverless platforms (Vercel Cron).

Enabled only when OBLAG_CRON_SECRET is set; Vercel sends `Authorization: Bearer
$CRON_SECRET` on cron invocations automatically when that env var exists. Self-hosted
deployments keep using the in-process APScheduler and never enable these."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from oblag.adapters import available_adapters, get_adapter
from oblag.config import get_settings
from oblag.core.reducer import tick as run_tick
from oblag.core.runner import run_adapter
from oblag.notify import dispatch_pending
from oblag.scheduler import ADAPTER_GROUPS
from oblag.web.deps import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/internal", include_in_schema=False)


def _authorize(request: Request) -> None:
    secret = get_settings().cron_secret
    if not secret:
        raise HTTPException(404, "cron endpoints are not enabled (set OBLAG_CRON_SECRET)")
    if request.headers.get("authorization") != f"Bearer {secret}":
        raise HTTPException(401, "bad or missing cron authorization")


def _stats_dict(stats) -> dict:
    return {
        "adapter": stats.adapter,
        "pages": stats.pages,
        "items": stats.items,
        "created": stats.created,
        "events": len(stats.events),
        "errors": stats.errors,
        "skipped": stats.skipped,
    }


def _run_one(db: Session, name: str, since_days: int) -> dict:
    since = datetime.now(UTC) - timedelta(days=since_days)
    return _stats_dict(run_adapter(db, name, since=since))


@router.get("/run/{adapter}")
def run_single(adapter: str, request: Request, since_days: int = 3, db: Session = Depends(get_db)):
    _authorize(request)
    if adapter not in available_adapters():
        raise HTTPException(404, f"unknown adapter {adapter!r}")
    result = _run_one(db, adapter, since_days=min(max(since_days, 1), 90))
    delivered = dispatch_pending(db)
    return {"run": result, "notifications_delivered": delivered}


@router.get("/purge-items")
def purge_items(ids: str, request: Request, db: Session = Depends(get_db)):
    """Maintenance: hard-delete corrupted pipeline items (with their dates, join keys,
    and events) so the next ingestion run re-creates them cleanly. Built for the
    umbrella-join-key merge corruption; usable for any bad-data repair."""
    _authorize(request)
    from oblag.db.models import Event, JoinKey, KeyDate, NotificationLog, PipelineItem

    try:
        item_ids = sorted({int(x) for x in ids.split(",") if x.strip()})
    except ValueError as exc:
        raise HTTPException(422, "ids must be a comma-separated list of integers") from exc
    if not item_ids:
        raise HTTPException(422, "no ids given")
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
    # unlink survivors pointing at purged items before the rows disappear
    db.query(PipelineItem).filter(PipelineItem.resolved_change_id.in_(found)).update(
        {PipelineItem.resolved_change_id: None}, synchronize_session=False
    )
    deleted_items = (
        db.query(PipelineItem).filter(PipelineItem.id.in_(found)).delete(synchronize_session=False)
    )
    db.commit()
    return {
        "purged_items": found,
        "not_found": sorted(set(item_ids) - set(found)),
        "deleted_events": deleted_events,
        "deleted_notifications": deleted_notifications,
        "deleted_item_rows": deleted_items,
    }


@router.get("/relink")
def relink_items(request: Request, db: Session = Depends(get_db)):
    """Maintenance: run the title-based obligation matcher over unlinked items
    (backfills data ingested before the fallback linker existed)."""
    _authorize(request)
    from oblag.db.models import Obligation, PipelineItem
    from oblag.linking import infer_obligation

    slug_ids = {slug: oid for oid, slug in db.query(Obligation.id, Obligation.slug)}
    linked = []
    for item in db.query(PipelineItem).filter(PipelineItem.obligation_id.is_(None)):
        slug = infer_obligation(item.title)
        if slug and slug in slug_ids:
            item.obligation_id = slug_ids[slug]
            linked.append({"item": item.id, "obligation": slug, "title": item.title[:80]})
    db.commit()
    return {"linked": linked, "count": len(linked)}


@router.get("/run-group/{group}")
def run_group(group: str, request: Request, db: Session = Depends(get_db)):
    _authorize(request)
    names = ADAPTER_GROUPS.get(group)
    if names is None:
        raise HTTPException(404, f"unknown group {group!r} (have: {sorted(ADAPTER_GROUPS)})")
    # weekly runs use a longer overlap window so a missed invocation never loses items
    since_days = 3 if group == "daily" else 10
    work = [(name, since_days) for name in names]
    weekly_included = False
    if group == "daily" and datetime.now(UTC).weekday() == 0:
        # Vercel Hobby allows only 2 cron jobs, so the weekly group has no schedule
        # of its own — it piggybacks on Monday's daily invocation instead.
        weekly_included = True
        work += [(name, 10) for name in ADAPTER_GROUPS.get("weekly", []) if name not in names]
    results = []
    for name, days in work:
        if name not in available_adapters() or not get_adapter(name).enabled():
            results.append({"adapter": name, "skipped": True})
            continue
        try:
            results.append(_run_one(db, name, days))
        except Exception as exc:  # noqa: BLE001 — one adapter never blocks the group
            log.exception("cron run failed for %s", name)
            results.append({"adapter": name, "error": str(exc)[:200]})
    delivered = dispatch_pending(db)
    return {
        "group": group,
        "runs": results,
        "weekly_included": weekly_included,
        "notifications_delivered": delivered,
    }


@router.get("/tick")
def tick_endpoint(request: Request, db: Session = Depends(get_db)):
    _authorize(request)
    events = run_tick(db)
    db.commit()
    delivered = dispatch_pending(db)
    return {"transitions": len(events), "notifications_delivered": delivered}


@router.get("/dispatch")
def dispatch_endpoint(request: Request, db: Session = Depends(get_db)):
    _authorize(request)
    return {"notifications_delivered": dispatch_pending(db)}
