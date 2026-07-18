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
def run_single(adapter: str, request: Request, db: Session = Depends(get_db)):
    _authorize(request)
    if adapter not in available_adapters():
        raise HTTPException(404, f"unknown adapter {adapter!r}")
    result = _run_one(db, adapter, since_days=3)
    delivered = dispatch_pending(db)
    return {"run": result, "notifications_delivered": delivered}


@router.get("/run-group/{group}")
def run_group(group: str, request: Request, db: Session = Depends(get_db)):
    _authorize(request)
    names = ADAPTER_GROUPS.get(group)
    if names is None:
        raise HTTPException(404, f"unknown group {group!r} (have: {sorted(ADAPTER_GROUPS)})")
    # weekly runs use a longer overlap window so a missed invocation never loses items
    since_days = 3 if group == "daily" else 10
    results = []
    for name in names:
        if name not in available_adapters() or not get_adapter(name).enabled():
            results.append({"adapter": name, "skipped": True})
            continue
        try:
            results.append(_run_one(db, name, since_days))
        except Exception as exc:  # noqa: BLE001 — one adapter never blocks the group
            log.exception("cron run failed for %s", name)
            results.append({"adapter": name, "error": str(exc)[:200]})
    delivered = dispatch_pending(db)
    return {"group": group, "runs": results, "notifications_delivered": delivered}


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
