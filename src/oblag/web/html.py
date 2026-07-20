from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from oblag.db.models import EventType, ItemState, KeyDate, PipelineItem
from oblag.web import api
from oblag.web.deps import get_db
from oblag.web.serialize import item_to_dict

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# ---- presentation helpers: raw enums/payloads never reach the user ----------

STATE_LABELS = {
    "proposed": "Proposed",
    "comment_open": "Comment open",
    "comment_closed": "Comment closed",
    "final_pending_effective": "Final · pending effective",
    "effective": "Effective",
    "withdrawn": "Withdrawn",
    "stalled": "Stalled",
    "superseded": "Superseded",
}
DATE_LABELS = {
    "proposal_date": "Proposed",
    "comment_open": "Comments open",
    "comment_close": "Comments close",
    "projected_final": "Projected final",
    "adopted": "Adopted",
    "effective": "Effective",
    "phased_compliance": "Phased compliance",
    "entry_into_force": "Entry into force",
    "application": "Application",
    "transition_deadline": "Transition deadline",
}
CONF_LABELS = {
    "statutory_hard": "Statutory",
    "published_firm": "Published",
    "agency_estimate": "Estimate",
    "derived": "Derived",
}
CONF_HELP = {
    "statutory_hard": "Fixed by statute — will not move without new legislation",
    "published_firm": "Published by the issuing body in an official document",
    "agency_estimate": "The agency's own projection — may slip",
    "derived": "Inferred by ObligationAggregator, not stated by the source",
}
SOURCE_LABELS = {
    "federal_register": "Federal Register",
    "nist_csrc": "NIST CSRC",
    "regulations_gov": "Regulations.gov",
    "cellar": "EUR-Lex",
    "oeil": "OEIL",
    "have_your_say": "EU Have Your Say",
    "legiscan": "LegiScan",
    "pci_ssc": "PCI SSC",
    "iso_catalog": "ISO",
    "edpb": "EDPB",
    "esma": "ESMA",
    "cppa": "CPPA",
    "eba": "EBA",
    "nerc": "NERC",
    "cis": "CIS",
    "aicpa": "AICPA",
    "hitrust": "HITRUST",
}
EVENT_LABELS = {
    "item_created": "Created",
    "state_changed": "State change",
    "date_changed": "Date change",
    "content_changed": "Content change",
    "item_resolved": "Resolved",
    "anomaly": "Anomaly",
}


def _human_state(value: str | None) -> str:
    return STATE_LABELS.get(value or "", (value or "").replace("_", " "))


def _human_date_type(value: str | None) -> str:
    return DATE_LABELS.get(value or "", (value or "").replace("_", " "))


def _human_conf(value: str | None) -> str:
    return CONF_LABELS.get(value or "", value or "")


def _human_source(value: str | None) -> str:
    return SOURCE_LABELS.get(value or "", value or "")


def _days_until(value: str | None) -> int | None:
    from datetime import date

    if not value:
        return None
    try:
        return (date.fromisoformat(value[:10]) - date.today()).days
    except ValueError:
        return None


def _reldate(value: str | None) -> str:
    d = _days_until(value)
    if d is None:
        return ""
    if d == 0:
        return "today"
    if d == 1:
        return "tomorrow"
    if d == -1:
        return "yesterday"
    if d > 0:
        return f"in {d} days"
    return f"{-d} days ago"


def _event_text(e: dict) -> str:
    p = e.get("payload") or {}
    t = e.get("type")
    if t == "state_changed":
        return f"{_human_state(p.get('from')) or 'New'} → {_human_state(p.get('to'))}"
    if t == "date_changed":
        frm = p.get("from") or "unset"
        return (
            f"{_human_date_type(p.get('date_type'))}: {frm} → {p.get('to')}"
            f" ({_human_conf(p.get('confidence'))})"
        )
    if t == "item_resolved":
        return f"Superseded by final document (item #{p.get('resolved_to')})"
    if t == "content_changed":
        return "Source content changed (new fingerprint)"
    if t == "item_created":
        return f"First seen via {_human_source(p.get('source'))}"
    if t == "anomaly":
        return f"{p.get('kind', 'anomaly')}: {p.get('detail', '')}"
    return ""


def _human_event(value: str | None) -> str:
    return EVENT_LABELS.get(value or "", (value or "").replace("_", " "))


templates.env.filters.update(
    human_event=_human_event,
    human_state=_human_state,
    human_date_type=_human_date_type,
    human_conf=_human_conf,
    human_source=_human_source,
    days_until=_days_until,
    reldate=_reldate,
    event_text=_event_text,
)
templates.env.globals.update(conf_help=CONF_HELP, state_labels=STATE_LABELS)


@router.get("/", response_class=HTMLResponse)
def items_page(
    request: Request,
    db: Session = Depends(get_db),
    state: str | None = None,
    source: str | None = None,
    q: str | None = None,
):
    data = api.list_items(
        db=db,
        state=[state] if state else None,
        source=[source] if source else None,
        jurisdiction=None,
        track=None,
        q=q,
        limit=100,
        offset=0,
    )
    sources = sorted(row[0] for row in db.query(PipelineItem.source_system).distinct())
    from sqlalchemy import func

    state_counts: dict[ItemState, int] = {
        row[0]: row[1]
        for row in db.query(PipelineItem.state, func.count()).group_by(PipelineItem.state)
    }
    stats = {
        "total": sum(state_counts.values()),
        "comment_open": state_counts.get(ItemState.comment_open, 0),
        "pending_effective": state_counts.get(ItemState.final_pending_effective, 0),
        "proposed": state_counts.get(ItemState.proposed, 0)
        + state_counts.get(ItemState.comment_closed, 0),
    }
    return templates.TemplateResponse(
        request,
        "items.html",
        {
            "items": data["items"],
            "total": data["total"],
            "states": [s.value for s in ItemState],
            "sources": sources,
            "state": state,
            "source": source,
            "q": q,
            "stats": stats,
        },
    )


@router.get("/items/{item_id}", response_class=HTMLResponse)
def item_page(item_id: int, request: Request, db: Session = Depends(get_db)):
    item = db.get(PipelineItem, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    superseded_ids = {
        r.supersedes_id
        for r in db.query(KeyDate.supersedes_id)
        .filter(KeyDate.pipeline_item_id == item_id, KeyDate.supersedes_id.isnot(None))
        .all()
    }
    return templates.TemplateResponse(
        request,
        "item_detail.html",
        {"item": item_to_dict(db, item, detail=True), "superseded_ids": superseded_ids},
    )


@router.get("/events", response_class=HTMLResponse)
def events_page(request: Request, db: Session = Depends(get_db), type: str | None = None):
    data = api.list_events(db=db, type=[type] if type else None, item_id=None, limit=200, offset=0)
    return templates.TemplateResponse(
        request,
        "events.html",
        {"events": data["events"], "types": [t.value for t in EventType], "type": type},
    )


@router.get("/deadlines", response_class=HTMLResponse)
def deadlines_page(request: Request, db: Session = Depends(get_db), within_days: int = 365):
    data = api.upcoming_deadlines(db=db, date_type=None, within_days=within_days)
    return templates.TemplateResponse(request, "deadlines.html", data)


@router.get("/watchlists", response_class=HTMLResponse)
def watchlists_page(request: Request, db: Session = Depends(get_db)):
    from oblag.web import watchlists as wl_api

    data = wl_api.list_watchlists(db=db)
    return templates.TemplateResponse(request, "watchlists.html", data)


@router.post("/watchlists", response_class=HTMLResponse)
async def watchlists_create(request: Request, db: Session = Depends(get_db)):
    from fastapi.responses import RedirectResponse

    from oblag.web import watchlists as wl_api

    form = await request.form()
    csv = lambda key: [s.strip() for s in str(form.get(key, "")).split(",") if s.strip()]  # noqa: E731
    body = wl_api.WatchlistIn(
        name=str(form.get("name", "unnamed")),
        channel=str(form.get("channel", "rss")),
        target=str(form.get("target") or "") or None,
        filters=wl_api.WatchlistFilters(
            source_systems=csv("source_systems"),
            states=csv("states"),
            event_types=csv("event_types"),
        ),
    )
    wl_api.create_watchlist(body, db=db)
    return RedirectResponse("/watchlists", status_code=303)


@router.post("/watchlists/{watchlist_id}/delete", response_class=HTMLResponse)
def watchlists_delete(watchlist_id: int, db: Session = Depends(get_db)):
    from fastapi.responses import RedirectResponse

    from oblag.web import watchlists as wl_api

    wl_api.delete_watchlist(watchlist_id, db=db)
    return RedirectResponse("/watchlists", status_code=303)


@router.get("/health", response_class=HTMLResponse)
def health_page(request: Request, db: Session = Depends(get_db)):
    data = api.adapter_health(db=db)
    return templates.TemplateResponse(request, "health.html", data)
