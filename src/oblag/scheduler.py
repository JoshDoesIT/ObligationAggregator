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
# Weekly sources: (weekday, HH:MM). Spread Mon–Fri (0–4) rather than piled onto Monday
# so the serverless daily cron only ever adds ~1–2 weekly runs to its window — a single
# invocation running all 18 adapters risks the 300s function timeout (some pull 10+
# pages). weekly_due_today() selects the ones due on a given weekday.
WEEKLY_ADAPTERS: dict[str, tuple[int, str]] = {
    "pci_ssc": (0, "08:10"),
    "iso_catalog": (1, "08:30"),
    "cppa": (1, "08:45"),
    "eba": (2, "09:00"),  # browser-rendered; self-disables without playwright
    "nerc": (2, "09:15"),
    "cis": (3, "09:30"),
    "aicpa": (3, "09:45"),  # sitemap-based (the landing SPA is broken upstream — spec 06)
    "hitrust": (4, "09:55"),  # sitemap-based (no feed, WP REST disabled)
}

# Groups reused by the serverless cron endpoints (web/internal.py)
ADAPTER_GROUPS: dict[str, list[str]] = {
    "daily": list(DAILY_ADAPTERS),
    "weekly": list(WEEKLY_ADAPTERS),
}


def weekly_due_today(weekday: int) -> list[str]:
    """Weekly adapters scheduled for the given weekday (0=Mon)."""
    return [name for name, (wd, _hhmm) in WEEKLY_ADAPTERS.items() if wd == weekday]


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
    _weekday_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    for name, (weekday, hhmm) in WEEKLY_ADAPTERS.items():
        if name not in available or not get_adapter(name).enabled():
            continue
        hour, minute = hhmm.split(":")
        scheduler.add_job(
            _run,
            CronTrigger(day_of_week=_weekday_names[weekday], hour=hour, minute=minute),
            args=[name],
            id=f"fetch-{name}",
        )
    scheduler.add_job(_run_tick, CronTrigger(hour="0", minute="15"), id="tick")
    return scheduler
