from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from oblag.adapters import available_adapters, get_adapter
from oblag.core.reducer import tick
from oblag.core.runner import run_adapter
from oblag.db.session import session_scope

log = logging.getLogger(__name__)

# Per-source polling cadence (plan §architecture): hour-of-day is spread so one bad
# source never blocks the rest; weekly sources run on Mondays.
DAILY_ADAPTERS = {
    "federal_register": "05:10",
    "regulations_gov": "05:40",
    "nist_csrc": "06:10",
    "cellar": "06:40",
    "oeil": "07:10",
    "have_your_say": "07:25",
    "legiscan": "07:40",
    "edpb": "07:50",
    "esma": "08:00",
}
WEEKLY_ADAPTERS: dict[str, str] = {
    "pci_ssc": "08:10",
    "iso_catalog": "08:30",
    "cppa": "08:45",
    "eba": "09:00",  # browser-rendered; self-disables without playwright
    "nerc": "09:15",
    "cis": "09:30",
    "aicpa": "09:45",  # sitemap-based (the landing SPA is broken upstream — spec 06)
    "hitrust": "09:55",  # sitemap-based (no feed, WP REST disabled)
}

# Groups reused by the serverless cron endpoints (web/internal.py)
ADAPTER_GROUPS: dict[str, list[str]] = {
    "daily": list(DAILY_ADAPTERS),
    "weekly": list(WEEKLY_ADAPTERS),
}


def _run(name: str) -> None:
    since = datetime.now(UTC) - timedelta(days=3)  # overlap window: never miss a slow index
    with session_scope() as session:
        stats = run_adapter(session, name, since=since)
        log.info(
            "adapter %s: pages=%d items=%d created=%d events=%d errors=%d",
            name,
            stats.pages,
            stats.items,
            stats.created,
            len(stats.events),
            len(stats.errors),
        )
    _dispatch_notifications()


def _run_tick() -> None:
    with session_scope() as session:
        events = tick(session)
        log.info("tick: %d time-based transitions", len(events))
    _dispatch_notifications()


def _dispatch_notifications() -> None:
    from oblag.notify import dispatch_pending

    with session_scope() as session:
        dispatch_pending(session)


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    available = set(available_adapters())
    for name, hhmm in DAILY_ADAPTERS.items():
        if name not in available or not get_adapter(name).enabled():
            continue
        hour, minute = hhmm.split(":")
        scheduler.add_job(
            _run, CronTrigger(hour=hour, minute=minute), args=[name], id=f"fetch-{name}"
        )
    for name, hhmm in WEEKLY_ADAPTERS.items():
        if name not in available or not get_adapter(name).enabled():
            continue
        hour, minute = hhmm.split(":")
        scheduler.add_job(
            _run,
            CronTrigger(day_of_week="mon", hour=hour, minute=minute),
            args=[name],
            id=f"fetch-{name}",
        )
    scheduler.add_job(_run_tick, CronTrigger(hour="0", minute="15"), id="tick")
    return scheduler
