"""Notification dispatch: watchlists → email / webhook (push) and RSS (pull).

Delivery contract (spec 01): at-most-once per (watchlist, event) enforced by the
notification_log unique constraint. RSS channels are pull-based — the feed endpoint
filters events live; no dispatch rows are written for them."""

from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx
from sqlalchemy.orm import Session

from oblag.config import get_settings
from oblag.db.models import Event, NotificationLog, PipelineItem, Watchlist

log = logging.getLogger(__name__)

MAX_EVENTS_PER_RUN = 500


def matches(watchlist: Watchlist, event: Event, item: PipelineItem | None) -> bool:
    f: dict[str, Any] = watchlist.filters or {}
    if f.get("event_types") and event.type.value not in f["event_types"]:
        return False
    if item is None:
        # system events match only watchlists with no item-scoped filters
        return not any(
            f.get(k) for k in ("source_systems", "jurisdictions", "states", "obligation_slugs")
        )
    if f.get("source_systems") and item.source_system not in f["source_systems"]:
        return False
    if f.get("jurisdictions") and item.jurisdiction not in f["jurisdictions"]:
        return False
    if f.get("states") and item.state.value not in f["states"]:
        return False
    if f.get("obligation_slugs"):
        slug = item.obligation.slug if item.obligation else None
        if slug not in f["obligation_slugs"]:
            return False
    return True


def _event_summary(event: Event, item: PipelineItem | None) -> str:
    title = item.title if item else "(system)"
    if event.type.value == "date_changed":
        p = event.payload
        return (
            f"[{event.type.value}] {title}: {p.get('date_type')} "
            f"{p.get('from') or '∅'} → {p.get('to')} ({p.get('confidence')})"
        )
    if event.type.value == "state_changed":
        p = event.payload
        return f"[{event.type.value}] {title}: {p.get('from') or '∅'} → {p.get('to')}"
    return f"[{event.type.value}] {title}"


def _deliver_webhook(watchlist: Watchlist, events: list[tuple[Event, PipelineItem | None]]) -> None:
    if not watchlist.target:
        raise ValueError("webhook watchlist has no target URL")
    from oblag.netguard import assert_safe_url

    assert_safe_url(watchlist.target)  # re-check at delivery time (DNS rebinding)
    base = get_settings().base_url.rstrip("/")
    payload = {
        "watchlist": watchlist.name,
        "events": [
            {
                "id": ev.id,
                "type": ev.type.value,
                "payload": ev.payload,
                "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
                "item": {
                    "id": item.id,
                    "title": item.title,
                    "state": item.state.value,
                    "url": f"{base}/items/{item.id}",
                }
                if item
                else None,
                "summary": _event_summary(ev, item),
            }
            for ev, item in events
        ],
    }
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}
    if watchlist.signing_secret:
        import hashlib
        import hmac

        digest = hmac.new(
            watchlist.signing_secret.encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        headers["X-Oblag-Signature"] = f"sha256={digest}"
    resp = httpx.post(
        watchlist.target,
        content=body,
        headers=headers,
        timeout=15.0,
        follow_redirects=False,  # a 302 to an internal host would defeat the SSRF guard
    )
    resp.raise_for_status()


def _deliver_email(watchlist: Watchlist, events: list[tuple[Event, PipelineItem | None]]) -> None:
    settings = get_settings()
    if not settings.smtp_host:
        raise RuntimeError("SMTP is not configured (OBLAG_SMTP_HOST)")
    if not watchlist.target:
        raise ValueError("email watchlist has no target address")
    base = settings.base_url.rstrip("/")
    lines = [
        f"{len(events)} new regulatory change event(s) for watchlist {watchlist.name!r}:",
        "",
    ]
    for ev, item in events:
        lines.append("• " + _event_summary(ev, item))
        if item:
            lines.append(f"  {base}/items/{item.id}")
    msg = EmailMessage()
    msg["Subject"] = f"[oblag] {len(events)} change event(s) — {watchlist.name}"
    msg["From"] = settings.smtp_from
    msg["To"] = watchlist.target
    msg.set_content("\n".join(lines))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        if settings.smtp_user and settings.smtp_password:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


def dispatch_pending(session: Session) -> int:
    """Deliver undelivered events to matching push watchlists. Returns deliveries made."""
    watchlists = (
        session.query(Watchlist)
        .filter(Watchlist.active.is_(True), Watchlist.channel.in_(["email", "webhook"]))
        .all()
    )
    if not watchlists:
        return 0
    delivered = 0
    for wl in watchlists:
        seen_ids = {
            row[0] for row in session.query(NotificationLog.event_id).filter_by(watchlist_id=wl.id)
        }
        query = session.query(Event).order_by(Event.id)
        if seen_ids:
            query = query.filter(Event.id.notin_(seen_ids))
        if wl.created_at is not None:
            query = query.filter(Event.occurred_at >= wl.created_at)
        candidates = query.limit(MAX_EVENTS_PER_RUN).all()
        batch: list[tuple[Event, PipelineItem | None]] = []
        for ev in candidates:
            item = session.get(PipelineItem, ev.pipeline_item_id) if ev.pipeline_item_id else None
            if matches(wl, ev, item):
                batch.append((ev, item))
        if not batch:
            continue
        try:
            if wl.channel == "webhook":
                _deliver_webhook(wl, batch)
            else:
                _deliver_email(wl, batch)
        except Exception as exc:  # noqa: BLE001 — transient failure: retry next run
            log.warning("delivery failed for watchlist %s: %s", wl.name, exc)
            continue
        # log only successes: a logged event is never redelivered (at-most-once);
        # unlogged events are retried on the next dispatch run
        for ev, _item in batch:
            session.add(NotificationLog(watchlist_id=wl.id, event_id=ev.id, status="sent"))
            delivered += 1
        session.flush()
    session.commit()
    return delivered
