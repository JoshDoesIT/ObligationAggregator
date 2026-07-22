"""Internal endpoints for scheduled ingestion on serverless platforms (Vercel Cron).

Enabled only when OBLAG_CRON_SECRET is set; Vercel sends `Authorization: Bearer
$CRON_SECRET` on cron invocations automatically when that env var exists. Self-hosted
deployments keep using the in-process APScheduler and never enable these."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from oblag.adapters import available_adapters, get_adapter
from oblag.config import get_settings
from oblag.core.reducer import tick as run_tick
from oblag.core.runner import run_adapter
from oblag.notify import alert_unhealthy_adapters, dispatch_pending
from oblag.scheduler import ADAPTER_GROUPS, weekly_due_today
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
    from oblag.maintenance import purge_items as _purge

    try:
        item_ids = sorted({int(x) for x in ids.split(",") if x.strip()})
    except ValueError as exc:
        raise HTTPException(422, "ids must be a comma-separated list of integers") from exc
    if not item_ids:
        raise HTTPException(422, "no ids given")
    result = _purge(db, item_ids)
    db.commit()
    found = result["purged_items"]
    assert isinstance(found, list)
    return {**result, "not_found": sorted(set(item_ids) - set(found))}


@router.get("/seed")
def seed_endpoint(request: Request, db: Session = Depends(get_db)):
    """Maintenance: upsert the obligation catalog (boot only seeds an EMPTY table,
    so catalog additions need an explicit run against existing databases)."""
    _authorize(request)
    from oblag.catalog import seed_obligations

    count = seed_obligations(db)
    db.commit()
    return {"seeded": count}


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


# Leave headroom under the function's maxDuration (vercel.json: 300s) so the invocation
# returns cleanly — committing what ran and recording the rest as deferred — instead of
# being killed mid-write. Deferred adapters run on the next daily invocation.
_GROUP_TIME_BUDGET_S = 240.0


@router.get("/run-group/{group}")
def run_group(group: str, request: Request, db: Session = Depends(get_db)):
    _authorize(request)
    names = ADAPTER_GROUPS.get(group)
    if names is None:
        raise HTTPException(404, f"unknown group {group!r} (have: {sorted(ADAPTER_GROUPS)})")
    # weekly runs use a longer overlap window so a missed invocation never loses items
    since_days = 3 if group == "daily" else 10
    work = [(name, since_days) for name in names]
    weekly_included: list[str] = []
    if group == "daily":
        # Weekly sources are spread Mon–Fri (scheduler.WEEKLY_ADAPTERS) so each daily
        # invocation only adds the ~1–2 due today — no Monday pile-up that risks the
        # function timeout. (Vercel Hobby's 2-cron limit means weekly has no schedule
        # of its own.)
        weekly_included = weekly_due_today(datetime.now(UTC).weekday())
        work += [(name, 10) for name in weekly_included if name not in names]

    start = monotonic()
    results = []
    deferred = []
    for name, days in work:
        if monotonic() - start > _GROUP_TIME_BUDGET_S:
            deferred.append(name)
            continue
        if name not in available_adapters() or not get_adapter(name).enabled():
            results.append({"adapter": name, "skipped": True})
            continue
        try:
            results.append(_run_one(db, name, days))
        except Exception as exc:  # noqa: BLE001 — one adapter never blocks the group
            log.exception("cron run failed for %s", name)
            results.append({"adapter": name, "error": str(exc)[:200]})
    delivered = dispatch_pending(db)
    alerted = alert_unhealthy_adapters(db)
    return {
        "group": group,
        "runs": results,
        "weekly_included": weekly_included,
        "deferred": deferred,
        "notifications_delivered": delivered,
        "ops_alerted": alerted,
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
