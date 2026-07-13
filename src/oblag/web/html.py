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
    sources = [row[0] for row in db.query(PipelineItem.source_system).distinct()]
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


@router.get("/health", response_class=HTMLResponse)
def health_page(request: Request, db: Session = Depends(get_db)):
    data = api.adapter_health(db=db)
    return templates.TemplateResponse(request, "health.html", data)
