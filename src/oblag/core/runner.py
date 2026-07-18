from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from oblag.adapters import get_adapter
from oblag.adapters.base import FetchContext, make_client
from oblag.core.linker import link_resolved_items
from oblag.core.reducer import reduce_item
from oblag.db.models import AdapterHealth, Event
from oblag.snapshots import SnapshotStore

log = logging.getLogger(__name__)


@dataclass
class RunStats:
    adapter: str
    pages: int = 0
    items: int = 0
    created: int = 0
    events: list[Event] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False


def _health(session: Session, adapter: str) -> AdapterHealth:
    row = session.query(AdapterHealth).filter_by(adapter=adapter).one_or_none()
    if row is None:
        row = AdapterHealth(adapter=adapter)
        session.add(row)
        session.flush()
    return row


def run_adapter(
    session: Session,
    name: str,
    *,
    since: datetime | None = None,
    window: tuple[date, date] | None = None,
    params: dict | None = None,
    today: date | None = None,
) -> RunStats:
    """Fetch → snapshot → normalize → reduce → link. One bad record never aborts the run."""
    stats = RunStats(adapter=name)
    adapter = get_adapter(name)
    if not adapter.enabled():
        log.info("adapter %s disabled (missing credentials?); skipping", name)
        stats.skipped = True
        return stats

    store = SnapshotStore.from_settings()
    health = _health(session, name)
    health.last_run_at = datetime.now(UTC)
    try:
        with make_client() as client:
            ctx = FetchContext(client=client, since=since, window=window, params=params or {})
            for raw in adapter.fetch_raw(ctx):
                stats.pages += 1
                headers = dict(raw.http_headers)
                if raw.meta.get("rendered"):
                    # browser-tier fetch: snapshot is a DOM serialization, not raw
                    # response bytes — recorded in provenance (spec 06 addendum)
                    headers["x-oblag-rendered"] = "true"
                snap = store.record(
                    session,
                    content=raw.content,
                    source_url=raw.url,
                    adapter=name,
                    http_status=raw.http_status,
                    http_headers=headers,
                    fetched_at=raw.fetched_at,
                )
                for ni in adapter.normalize(raw):
                    try:
                        res = reduce_item(session, ni, snapshot_id=snap.id, today=today)
                        stats.items += 1
                        stats.created += int(res.created)
                        stats.events.extend(res.events)
                    except Exception as exc:  # noqa: BLE001 — per-item isolation
                        session.rollback()
                        msg = f"reduce failed for {ni.external_key}: {exc}"
                        log.exception(msg)
                        stats.errors.append(msg)
        stats.events.extend(link_resolved_items(session))
    except Exception as exc:  # fetch-level failure
        session.rollback()
        health = _health(session, name)
        health.last_run_at = datetime.now(UTC)
        health.consecutive_failures += 1
        health.last_error = f"{exc}\n{traceback.format_exc(limit=3)}"
        session.commit()
        stats.errors.append(str(exc))
        log.exception("adapter %s run failed", name)
        return stats

    health = _health(session, name)
    health.last_success_at = datetime.now(UTC)
    health.consecutive_failures = 0
    health.last_error = None
    health.items_seen_last_run = stats.items
    session.commit()
    return stats
