from __future__ import annotations

import secrets
from datetime import UTC, datetime
from email.utils import format_datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from oblag import notify
from oblag.db.models import Event, Org, PipelineItem, Watchlist
from oblag.notify import _event_summary, matches
from oblag.web.deps import Context, get_context, get_db

router = APIRouter(prefix="/api/v1")


def require_org(ctx: Context) -> Org:
    """The tenant for this request. Present in single-org mode (default org); in
    magic-link mode requires a logged-in user with an active org."""
    if ctx.org is None:
        raise HTTPException(401, "authentication required")
    return ctx.org


class WatchlistFilters(BaseModel):
    source_systems: list[str] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)
    obligation_slugs: list[str] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)
    # Bind to the org's obligation scope LIVE rather than copying it: the watchlist
    # then tracks edits to "obligations I'm subject to" instead of drifting out of
    # date, and the same list is never maintained in two places.
    use_org_scope: bool = False


class WatchlistIn(BaseModel):
    name: str
    channel: str = Field(pattern="^(rss|email|webhook)$")
    target: str | None = None
    filters: WatchlistFilters = Field(default_factory=WatchlistFilters)


def _to_dict(wl: Watchlist, request: Request | None = None) -> dict:
    from oblag.web.urls import site_base

    # An RSS URL is meant to be pasted into a reader on another machine, so it has to
    # be reachable from outside. base_url is still the localhost default on any
    # deployment that never set OBLAG_BASE_URL — Vercel included, which is why these
    # were being shown as http://localhost:8000/rss/....
    base = site_base(request)
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


def _owned(db: Session, watchlist_id: int, org: Org) -> Watchlist:
    """A live watchlist this org owns. Another org's row, or a deleted one, is a 404 —
    never a different error, which would confirm the id exists."""
    wl = db.get(Watchlist, watchlist_id)
    if wl is None or wl.org_id != org.id or wl.deleted_at is not None:
        raise HTTPException(404, "watchlist not found")
    return wl


@router.get("/watchlists")
def list_watchlists(
    request: Request = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    org = require_org(ctx)
    rows = (
        db.query(Watchlist)
        .filter(Watchlist.org_id == org.id, Watchlist.deleted_at.is_(None))
        .order_by(Watchlist.id)
    )
    return {"watchlists": [_to_dict(w, request) for w in rows]}


@router.post("/watchlists", status_code=201)
def create_watchlist(
    body: WatchlistIn,
    request: Request = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    org = require_org(ctx)
    from oblag.auth import QuotaError, enforce_quota

    try:
        enforce_quota(db, org.id, "watchlists")
    except QuotaError as exc:
        raise HTTPException(409, str(exc)) from None
    if body.channel in ("email", "webhook") and not body.target:
        raise HTTPException(422, f"{body.channel} watchlists require a target")
    if body.channel == "email" and not notify.email_enabled():
        # Refusing beats accepting one that can never deliver: the old behaviour saved
        # it, showed it as active, and dropped every digest on the floor.
        raise HTTPException(
            422,
            "email delivery is not configured on this instance, so an email watchlist "
            "could never be delivered. Use an RSS feed or a webhook, or set the mail "
            "settings first.",
        )
    target = body.target
    signing_secret = None
    if body.channel == "rss":
        target = secrets.token_urlsafe(16)  # unguessable pull token
    elif body.channel == "webhook":
        from oblag.netguard import UnsafeUrlError, assert_safe_url

        try:
            assert_safe_url(body.target or "")
        except UnsafeUrlError as exc:
            raise HTTPException(422, str(exc)) from None
        signing_secret = secrets.token_hex(32)  # HMAC key for X-Oblag-Signature
    wl = Watchlist(
        org_id=org.id,
        name=body.name,
        channel=body.channel,
        target=target,
        signing_secret=signing_secret,
        filters=body.filters.model_dump(),
        active=True,
    )
    db.add(wl)
    db.flush()
    return _to_dict(wl, request)


@router.post("/watchlists/{watchlist_id}/pause")
def pause_watchlist(
    watchlist_id: int,
    request: Request = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    """Stop delivering, keep everything. Reversible with /resume."""
    wl = _owned(db, watchlist_id, require_org(ctx))
    wl.active = False
    db.flush()
    return _to_dict(wl, request)


@router.post("/watchlists/{watchlist_id}/resume")
def resume_watchlist(
    watchlist_id: int,
    request: Request = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    """Start delivering again. The RSS token is unchanged, so a reader that stayed
    subscribed through the pause simply starts seeing entries again."""
    wl = _owned(db, watchlist_id, require_org(ctx))
    wl.active = True
    db.flush()
    return _to_dict(wl, request)


@router.delete("/watchlists/{watchlist_id}", status_code=204)
def delete_watchlist(
    watchlist_id: int, db: Session = Depends(get_db), ctx: Context = Depends(get_context)
):
    """Gone for good. The row survives so notification_log keeps its foreign key and
    the record of what was already sent stays intact, but nothing lists or delivers it
    and the feed answers 410 rather than pretending the token was never real."""
    wl = _owned(db, watchlist_id, require_org(ctx))
    wl.active = False
    wl.deleted_at = datetime.now(UTC)
    return Response(status_code=204)


rss_router = APIRouter(include_in_schema=False)


@rss_router.get("/rss/{token}.xml")
def rss_feed(token: str, request: Request, db: Session = Depends(get_db)):
    wl = db.query(Watchlist).filter_by(channel="rss", target=token).first()
    if wl is None:
        raise HTTPException(404, "unknown feed")
    if wl.deleted_at is not None:
        # 410, not 404: the token WAS real, and a reader that gets "gone" removes the
        # subscription cleanly instead of retrying a URL that will never come back.
        raise HTTPException(410, "this feed was deleted")
    # same reason as _to_dict: a feed read in someone's reader must link somewhere
    # reachable, and base_url is the localhost default until a deployment sets it
    from oblag.web.urls import site_base

    base = site_base(request)
    # A paused feed stays a FEED. Answering 404 made every reader say "unknown feed",
    # which reads as a broken link rather than a setting the owner chose, and there was
    # no way back short of re-subscribing. An empty channel that says why is honest and
    # resumes on its own the moment the watchlist is switched back on.
    events = (
        db.query(Event).order_by(Event.id.desc()).limit(500).all()
        if wl.active
        else []  # paused: no entries, but a well-formed channel
    )
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
    description = (
        "Regulatory change events"
        if wl.active
        else "This watchlist is paused. Resume it in Gazette and entries appear here again."
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        f"<title>Gazette — {escape(wl.name)}</title>"
        f"<link>{escape(base)}/watchlists</link>"
        f"<description>{escape(description)}</description>" + "".join(entries) + "</channel></rss>"
    )
    return Response(content=xml, media_type="application/rss+xml")
