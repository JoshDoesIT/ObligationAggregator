from __future__ import annotations

import secrets
from datetime import UTC, datetime
from email.utils import format_datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from oblag.config import get_settings
from oblag.db.models import Event, PipelineItem, Watchlist
from oblag.notify import _event_summary, matches
from oblag.web.deps import get_db

router = APIRouter(prefix="/api/v1")


class WatchlistFilters(BaseModel):
    source_systems: list[str] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)
    obligation_slugs: list[str] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)


class WatchlistIn(BaseModel):
    name: str
    channel: str = Field(pattern="^(rss|email|webhook)$")
    target: str | None = None
    filters: WatchlistFilters = Field(default_factory=WatchlistFilters)


def _to_dict(wl: Watchlist) -> dict:
    base = get_settings().base_url.rstrip("/")
    d = {
        "id": wl.id,
        "name": wl.name,
        "channel": wl.channel,
        "target": wl.target,
        "filters": wl.filters,
        "active": wl.active,
    }
    if wl.channel == "rss":
        d["feed_url"] = f"{base}/rss/{wl.target}.xml"
    return d


@router.get("/watchlists")
def list_watchlists(db: Session = Depends(get_db)):
    return {"watchlists": [_to_dict(w) for w in db.query(Watchlist).order_by(Watchlist.id)]}


@router.post("/watchlists", status_code=201)
def create_watchlist(body: WatchlistIn, db: Session = Depends(get_db)):
    if body.channel in ("email", "webhook") and not body.target:
        raise HTTPException(422, f"{body.channel} watchlists require a target")
    target = body.target
    if body.channel == "rss":
        target = secrets.token_urlsafe(16)  # unguessable pull token
    wl = Watchlist(
        name=body.name,
        channel=body.channel,
        target=target,
        filters=body.filters.model_dump(),
        active=True,
    )
    db.add(wl)
    db.flush()
    return _to_dict(wl)


@router.delete("/watchlists/{watchlist_id}", status_code=204)
def delete_watchlist(watchlist_id: int, db: Session = Depends(get_db)):
    wl = db.get(Watchlist, watchlist_id)
    if wl is None:
        raise HTTPException(404, "watchlist not found")
    wl.active = False  # soft delete keeps the notification audit log intact
    return Response(status_code=204)


rss_router = APIRouter(include_in_schema=False)


@rss_router.get("/rss/{token}.xml")
def rss_feed(token: str, db: Session = Depends(get_db)):
    wl = (
        db.query(Watchlist)
        .filter_by(channel="rss", target=token)
        .filter(Watchlist.active.is_(True))
        .one_or_none()
    )
    if wl is None:
        raise HTTPException(404, "unknown feed")
    base = get_settings().base_url.rstrip("/")
    events = db.query(Event).order_by(Event.id.desc()).limit(500).all()
    entries: list[str] = []
    for ev in events:
        item = db.get(PipelineItem, ev.pipeline_item_id) if ev.pipeline_item_id else None
        if not matches(wl, ev, item):
            continue
        link = f"{base}/items/{item.id}" if item else base
        when = format_datetime(
            ev.occurred_at.replace(tzinfo=UTC) if ev.occurred_at else datetime.now(UTC)
        )
        entries.append(
            "<item>"
            f"<title>{escape(_event_summary(ev, item))}</title>"
            f"<link>{escape(link)}</link>"
            f'<guid isPermaLink="false">oblag-event-{ev.id}</guid>'
            f"<pubDate>{when}</pubDate>"
            "</item>"
        )
        if len(entries) >= 100:
            break
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        f"<title>oblag — {escape(wl.name)}</title>"
        f"<link>{escape(base)}</link>"
        "<description>Regulatory change events</description>"
        + "".join(entries)
        + "</channel></rss>"
    )
    return Response(content=xml, media_type="application/rss+xml")
