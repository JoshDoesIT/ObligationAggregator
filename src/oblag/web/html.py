from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from oblag.db.models import EventType, ItemState, KeyDate, PipelineItem
from oblag.web import api
from oblag.web.deps import Context, check_csrf, get_context, get_db
from oblag.web.serialize import item_to_dict

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _login_redirect(ctx: Context) -> RedirectResponse | None:
    """In magic-link mode, send an unauthenticated visitor to sign-in and a logged-in
    user without an org to onboarding. No-op in single-org mode."""
    if not ctx.auth_on:
        return None
    if not ctx.authed:
        return RedirectResponse("/auth/login", status_code=303)
    if ctx.org is None:
        return RedirectResponse("/auth/onboarding", status_code=303)
    return None


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
    "curated": "Curated",
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
        if p.get("retracted"):
            return (
                f"{_human_date_type(p.get('date_type'))}: {frm} withdrawn — "
                "the source no longer states this date"
            )
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


# What KIND of change signal is this item? Advisories, drafts, consultations and
# rulemakings are different things — the change feed labels each row so it never
# reads as a list of "obligations".
_NIST_DRAFT_STAGES = {"iwd", "iprd", "prd", "2prd", "ipd", "2pd", "3pd", "fpd"}


def _signal_kind(item: dict) -> str:
    src = item.get("source_system") or ""
    native = (item.get("native_status") or "").lower()
    if src in ("federal_register", "regulations_gov"):
        return "Proposed rule" if native == "prorule" else "Final rule"
    if src == "nist_csrc":
        return "Draft standard" if native in _NIST_DRAFT_STAGES else "Publication"
    if src == "cellar":
        return "Proposed act" if item.get("track") == "proposed" else "EU act"
    if src in ("have_your_say", "edpb", "esma", "eba"):
        return "Consultation"
    if src == "cppa":
        return "Rulemaking" if native == "proposed" else "Rulemaking package"
    if src == "pci_ssc":
        return "Publication" if native == "publication" else "RFC"
    if src == "iso_catalog":
        return "Standard revision"
    if src == "legiscan":
        return "State bill"
    if src == "aicpa":
        return "Exposure draft"
    if src == "nerc":
        return "Standards project"
    if src in ("cis",) or native == "release":
        return "Version release"
    if native == "advisory":
        return "Advisory"
    if src == "curated" and native == "timeline":
        return "Timeline"
    return "Change signal"


# Signal kinds that are CONSULTATIONS on maintenance of a standard: RFCs, revision
# projects, exposure drafts, draft revisions. Whether one revises an in-force standard
# (vs drafting a first version) is decided against the catalog — never assumed.
_REVISION_SIGNAL_KINDS = {
    "RFC",  # PCI SSC
    "Standard revision",  # ISO
    "Exposure draft",  # AICPA
    "Version release",  # CIS, and any native "release" (pre-effective stages only)
    "Draft standard",  # NIST CSRC drafts (of an existing SP)
}
# Signal kinds that announce a PUBLISHED document. Once effective, these are facts
# about a released version — a consultation banner ("solicits feedback", "draft of
# the next version") on them misstates what the item is.
_RELEASE_SIGNAL_KINDS = {"Version release", "Publication", "Standard revision", "RFC"}
_CONSULT_STATES = ("proposed", "comment_open", "comment_closed")


def _revision_flavor(item: dict) -> str | None:
    """Which standards-maintenance CONSULTATION lifecycle an item gets, if any.

    PCI (and others) run consultations in three flavors, and only the catalog knows
    which: the obligation's in-force version.

    - None: not a maintenance consultation (wrong kind, no cataloged obligation, no
      published version — a first-version draft keeps the ordinary lifecycle — or the
      item is no longer in a consultation state: released/superseded/withdrawn items
      must not carry consultation wording).
    - "current": feedback solicited on the in-force version itself (PCI DSS v4.0.1 RFC).
    - "draft": the document under review is a draft of the NEXT version (PTS HSM v5.0
      RFC while v4.0 is in force) — the current version stays in force regardless.
    - "revision": maintenance of a published standard, but the title carries no
      comparable version token (ISO revisions, most NIST drafts) — the in-force claim
      holds; which flavor of it is unknown.
    """
    if item.get("state") not in _CONSULT_STATES:
        return None
    if _signal_kind(item) not in _REVISION_SIGNAL_KINDS:
        return None
    if not item.get("obligation"):
        return None
    current = item.get("obligation_current_version")
    if not current:
        return None
    from oblag.versions import version_key

    subject = version_key(item.get("title"))
    if subject is None:
        return "revision"
    return "current" if subject == version_key(current) else "draft"


def _release_status(item: dict) -> str | None:
    """Truthful framing for EFFECTIVE items about a cataloged standard.

    A published release/edition is a fact, not a consultation — the banner must say
    where that version stands today, and no comment-window stepper applies:

    - "informational": an advisory — commentary from the issuing body, not a
      lifecycle change to the standard itself.
    - "current": this item announces the version that is in force right now.
    - "superseded": it announced an older version; the in-force one is newer.
    - "published": version relationship unknown (no comparable tokens) — say only
      that the standard is published and what the current version is.
    - None: not an effective release-kind item about a versioned obligation.
    """
    if item.get("state") != "effective":
        return None
    kind = _signal_kind(item)
    if kind == "Advisory":
        return "informational"
    if kind not in _RELEASE_SIGNAL_KINDS:
        return None
    if not item.get("obligation") or not item.get("obligation_current_version"):
        return None
    from oblag.versions import version_key

    meta = item.get("native_meta") or {}
    subject = (
        version_key(meta.get("published_version"))
        # ISO publication dates look like "2022" or "2022-10": the year is the edition
        or version_key(str(meta.get("publication_date") or "")[:4])
        or version_key(item.get("title"))
    )
    current = version_key(item.get("obligation_current_version"))
    if subject is None or current is None:
        return "published"
    if subject == current:
        return "current"
    return "superseded" if subject < current else "published"


templates.env.filters.update(
    signal_kind=_signal_kind,
    revision_flavor=_revision_flavor,
    release_status=_release_status,
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
    obligation: str | None = None,
    q: str | None = None,
    page: int = 1,
    ctx: Context = Depends(get_context),
):
    page = max(page, 1)
    page_size = 50
    data = api.list_items(
        db=db,
        state=[state] if state else None,
        source=[source] if source else None,
        jurisdiction=None,
        track=None,
        q=q,
        obligation=obligation,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    sources = sorted(row[0] for row in db.query(PipelineItem.source_system).distinct())
    from sqlalchemy import func

    state_counts: dict[ItemState, int] = {
        row[0]: row[1]
        for row in db.query(PipelineItem.state, func.count()).group_by(PipelineItem.state)
    }
    deadlines_30d = len(api.upcoming_deadlines(db=db, date_type=None, within_days=30)["deadlines"])
    stats = {
        "total": sum(state_counts.values()),
        "comment_open": state_counts.get(ItemState.comment_open, 0),
        "pending_effective": state_counts.get(ItemState.final_pending_effective, 0),
        "deadlines_30d": deadlines_30d,
    }

    # Attention-first default ordering: open comment windows (nearest close first),
    # then finals awaiting effectiveness, then fresh proposals; historical/terminal
    # states sink. Explicit filters keep the API's recency order within the subset.
    _state_rank = {
        "comment_open": 0,
        "final_pending_effective": 1,
        "proposed": 2,
        "comment_closed": 3,
        "effective": 4,
        "stalled": 5,
        "superseded": 6,
        "withdrawn": 7,
    }

    def _next_deadline(it: dict) -> str:
        from datetime import date as _date

        future = [
            d["value"]
            for d in it.get("current_dates", [])
            if d["value"] >= _date.today().isoformat()
        ]
        return min(future) if future else "9999-12-31"

    items_sorted = sorted(
        data["items"],
        key=lambda it: (_state_rank.get(it["state"], 8), _next_deadline(it), -it["id"]),
    )
    from oblag.db.models import Obligation

    linked_obligations = [
        {"slug": slug, "name": name}
        for slug, name in db.query(Obligation.slug, Obligation.name)
        .join(PipelineItem, PipelineItem.obligation_id == Obligation.id)
        .distinct()
        .order_by(Obligation.slug)
    ]
    return templates.TemplateResponse(
        request,
        "items.html",
        {
            "items": items_sorted,
            "total": data["total"],
            "states": [s.value for s in ItemState],
            "sources": sources,
            "state": state,
            "source": source,
            "obligation": obligation,
            "obligations": linked_obligations,
            "q": q,
            "stats": stats,
            "page": page,
            "page_size": page_size,
            "has_next": page * page_size < data["total"],
        },
    )


@router.get("/obligations", response_class=HTMLResponse)
def obligations_page(
    request: Request, db: Session = Depends(get_db), ctx: Context = Depends(get_context)
):
    """The actual obligation catalog: frameworks/regulations a GRC team is subject to,
    each with its live change activity. Items on the change feed are signals ABOUT
    these — never obligations themselves."""
    from sqlalchemy import case, func

    from oblag.db.models import Obligation

    obligations = db.query(Obligation).order_by(Obligation.name).all()
    active_states = [
        ItemState.proposed,
        ItemState.comment_open,
        ItemState.comment_closed,
        ItemState.final_pending_effective,
    ]
    counts = {
        row[0]: (row[1], row[2])
        for row in db.query(
            PipelineItem.obligation_id,
            func.count(),
            func.sum(case((PipelineItem.state.in_(active_states), 1), else_=0)),
        )
        .filter(PipelineItem.obligation_id.isnot(None))
        .group_by(PipelineItem.obligation_id)
    }
    # next upcoming deadline per obligation (reuses the deadline resolution rules)
    deadline_data = api.upcoming_deadlines(db=db, date_type=None, within_days=3650)
    item_obligation = {
        row[0]: row[1]
        for row in db.query(PipelineItem.id, PipelineItem.obligation_id).filter(
            PipelineItem.obligation_id.isnot(None)
        )
    }
    next_deadline: dict[int, dict] = {}
    for d in deadline_data["deadlines"]:  # already sorted soonest-first
        ob_id = item_obligation.get(d["item_id"])
        if ob_id is not None and ob_id not in next_deadline:
            next_deadline[ob_id] = d
    rows = []
    for o in obligations:
        total, active = counts.get(o.id, (0, 0))
        rows.append(
            {
                "slug": o.slug,
                "name": o.name,
                "issuing_body": o.issuing_body,
                "jurisdiction": o.jurisdiction,
                "canonical_url": o.canonical_url,
                "total_items": total,
                "active_items": int(active or 0),
                "next_deadline": next_deadline.get(o.id),
            }
        )
    # obligations with activity first, most active on top
    rows.sort(key=lambda r: (-r["active_items"], -r["total_items"], r["name"]))
    return templates.TemplateResponse(request, "obligations.html", {"rows": rows})


@router.get("/items/{item_id}", response_class=HTMLResponse)
def item_page(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    item = db.get(PipelineItem, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    superseded_ids = {
        r.supersedes_id
        for r in db.query(KeyDate.supersedes_id)
        .filter(KeyDate.pipeline_item_id == item_id, KeyDate.supersedes_id.isnot(None))
        .all()
    }
    from oblag.db.models import Confidence, DateType

    return templates.TemplateResponse(
        request,
        "item_detail.html",
        {
            "item": item_to_dict(db, item, detail=True),
            "superseded_ids": superseded_ids,
            "ctx": ctx,
            "date_types": [d.value for d in DateType],
            "confidences": [c.value for c in Confidence],
        },
    )


@router.get("/admin/unlock", response_class=HTMLResponse)
def admin_unlock_page(request: Request, ctx: Context = Depends(get_context)):
    """Operator sign-in for single-org deployments guarded by OBLAG_ADMIN_TOKEN.
    404 when no token is configured (open mode) or auth is on (use the login flow)."""
    from oblag.auth import auth_enabled
    from oblag.web.deps import admin_gate_token

    if auth_enabled() or not admin_gate_token():
        raise HTTPException(404, "not found")
    return templates.TemplateResponse(
        request, "admin_unlock.html", {"ctx": ctx, "already": ctx.is_admin}
    )


@router.post("/admin/unlock", response_class=HTMLResponse)
async def admin_unlock(request: Request):
    import secrets as _secrets

    from oblag.auth import auth_enabled
    from oblag.web.deps import ADMIN_COOKIE, admin_gate_token

    token = admin_gate_token()
    if auth_enabled() or not token:
        raise HTTPException(404, "not found")
    form = await request.form()
    if not _secrets.compare_digest(str(form.get("token", "")), token):
        return templates.TemplateResponse(
            request,
            "admin_unlock.html",
            {"ctx": None, "error": "Incorrect token."},
            status_code=403,
        )
    resp = RedirectResponse("/", status_code=303)
    # httponly so JS can't read it; the operator presents it on subsequent writes
    resp.set_cookie(ADMIN_COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 12)
    return resp


@router.post("/admin/lock", response_class=HTMLResponse)
async def admin_lock():
    from oblag.web.deps import ADMIN_COOKIE

    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


@router.post("/items/{item_id}/assert-date", response_class=HTMLResponse)
async def assert_date_route(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    """Curated date assertion from the UI. Writes SHARED pipeline data, so it's gated
    to instance admins (single-org operators are admins)."""
    from datetime import date as _date

    from oblag.core.assertions import assert_date
    from oblag.db.models import Confidence, DateType

    if not ctx.is_admin:
        raise HTTPException(403, "instance-admin only")
    if db.get(PipelineItem, item_id) is None:
        raise HTTPException(404, "item not found")
    form = await request.form()
    check_csrf(ctx, str(form.get("csrf_token", "")))
    try:
        assert_date(
            db,
            item_id,
            DateType(str(form.get("date_type"))),
            _date.fromisoformat(str(form.get("value"))),
            Confidence(str(form.get("confidence"))),
            label=str(form.get("label") or "") or None,
            note=f"curated via UI by {ctx.user_email or 'operator'}",
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, f"invalid assertion: {exc}") from None
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@router.get("/admin/versions", response_class=HTMLResponse)
def versions_review_page(
    request: Request, db: Session = Depends(get_db), ctx: Context = Depends(get_context)
):
    """Read-only audit of the automatic version tracker: what it advanced and what it
    flagged as implausible (left for a catalog edit). Admin-gated — shared data."""
    from oblag import versionsuggest

    if not ctx.is_admin:
        raise HTTPException(403, "instance-admin only")
    return templates.TemplateResponse(
        request,
        "admin_versions.html",
        {"log": versionsuggest.version_log(db), "ctx": ctx},
    )


@router.post("/items/{item_id}/watch", response_class=HTMLResponse)
async def quick_watch(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    """One-click watch: RSS watchlist scoped to the item's obligation (or source)."""
    if r := _login_redirect(ctx):
        return r
    form = await request.form()
    check_csrf(ctx, str(form.get("csrf_token", "")))
    from oblag.web import watchlists as wl_api
    from oblag.web.watchlists import require_org

    org = require_org(ctx)
    item = db.get(PipelineItem, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    if item.obligation is not None:
        name = f"Watch: {item.obligation.name}"
        filters = wl_api.WatchlistFilters(obligation_slugs=[item.obligation.slug])
    else:
        name = f"Watch: {_human_source(item.source_system)}"
        filters = wl_api.WatchlistFilters(source_systems=[item.source_system])
    from oblag.db.models import Watchlist

    exists = db.query(Watchlist).filter_by(name=name, active=True, org_id=org.id).first()
    if exists is None:
        wl_api.create_watchlist(
            wl_api.WatchlistIn(name=name, channel="rss", target=None, filters=filters),
            db=db,
            ctx=ctx,
        )
    return RedirectResponse("/watchlists", status_code=303)


@router.get("/events", response_class=HTMLResponse)
def events_page(
    request: Request,
    db: Session = Depends(get_db),
    type: str | None = None,
    ctx: Context = Depends(get_context),
):
    data = api.list_events(db=db, type=[type] if type else None, item_id=None, limit=200, offset=0)
    item_ids = {e["item_id"] for e in data["events"] if e.get("item_id")}
    titles = {}
    if item_ids:
        titles = {
            row[0]: row[1]
            for row in db.query(PipelineItem.id, PipelineItem.title).filter(
                PipelineItem.id.in_(item_ids)
            )
        }
    return templates.TemplateResponse(
        request,
        "events.html",
        {
            "events": data["events"],
            "types": [t.value for t in EventType],
            "type": type,
            "titles": titles,
        },
    )


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


@router.get("/deadlines.ics")
def deadlines_ics(
    request: Request,
    db: Session = Depends(get_db),
    within_days: int = 365,
    ctx: Context = Depends(get_context),
):
    """Deadlines as an iCalendar feed — subscribe from Outlook/Google/Apple Calendar."""
    from datetime import date, timedelta

    from fastapi.responses import Response

    data = api.upcoming_deadlines(db=db, date_type=None, within_days=min(within_days, 3650))
    base = str(request.base_url).rstrip("/")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ObligationAggregator//deadlines//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Regulatory deadlines",
    ]
    for d in data["deadlines"]:
        start = date.fromisoformat(d["value"])
        end = start + timedelta(days=1)
        summary = f"{_human_date_type(d['date_type'])}: {d['title']}"
        desc = (
            f"Confidence: {_human_conf(d['confidence'])} · "
            f"State: {_human_state(d['state'])} · {base}/items/{d['item_id']}"
        )
        lines += [
            "BEGIN:VEVENT",
            f"UID:oblag-{d['item_id']}-{d['date_type']}-{d['value']}@obligation-aggregator",
            f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(desc)}",
            f"URL:{base}/items/{d['item_id']}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return Response(
        "\r\n".join(lines) + "\r\n",
        media_type="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="oblag-deadlines.ics"'},
    )


@router.get("/deadlines", response_class=HTMLResponse)
def deadlines_page(
    request: Request,
    db: Session = Depends(get_db),
    within_days: int = 365,
    ctx: Context = Depends(get_context),
):
    from oblag.watch import pending_outcomes

    data = api.upcoming_deadlines(db=db, date_type=None, within_days=within_days)
    data["watch"] = pending_outcomes(db)
    return templates.TemplateResponse(request, "deadlines.html", data)


@router.get("/watchlists", response_class=HTMLResponse)
def watchlists_page(
    request: Request, db: Session = Depends(get_db), ctx: Context = Depends(get_context)
):
    if r := _login_redirect(ctx):
        return r
    from oblag.db.models import Obligation
    from oblag.web import watchlists as wl_api

    data = wl_api.list_watchlists(db=db, ctx=ctx)
    data["obligations"] = [
        {"slug": slug, "name": name}
        for slug, name in db.query(Obligation.slug, Obligation.name).order_by(Obligation.name)
    ]
    data["ctx"] = ctx
    return templates.TemplateResponse(request, "watchlists.html", data)


@router.post("/watchlists", response_class=HTMLResponse)
async def watchlists_create(
    request: Request, db: Session = Depends(get_db), ctx: Context = Depends(get_context)
):
    if r := _login_redirect(ctx):
        return r
    from oblag.web import watchlists as wl_api

    form = await request.form()
    check_csrf(ctx, str(form.get("csrf_token", "")))
    csv = lambda key: [s.strip() for s in str(form.get(key, "")).split(",") if s.strip()]  # noqa: E731
    body = wl_api.WatchlistIn(
        name=str(form.get("name", "unnamed")),
        channel=str(form.get("channel", "rss")),
        target=str(form.get("target") or "") or None,
        filters=wl_api.WatchlistFilters(
            source_systems=csv("source_systems"),
            states=csv("states"),
            event_types=csv("event_types"),
            obligation_slugs=[str(v) for v in form.getlist("obligation_slugs")],
        ),
    )
    wl_api.create_watchlist(body, db=db, ctx=ctx)
    return RedirectResponse("/watchlists", status_code=303)


@router.post("/watchlists/{watchlist_id}/delete", response_class=HTMLResponse)
async def watchlists_delete(
    watchlist_id: int,
    request: Request,
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    if r := _login_redirect(ctx):
        return r
    from oblag.web import watchlists as wl_api

    form = await request.form()
    check_csrf(ctx, str(form.get("csrf_token", "")))
    wl_api.delete_watchlist(watchlist_id, db=db, ctx=ctx)
    return RedirectResponse("/watchlists", status_code=303)


@router.get("/health", response_class=HTMLResponse)
def health_page(
    request: Request, db: Session = Depends(get_db), ctx: Context = Depends(get_context)
):
    data = api.adapter_health(db=db)
    return templates.TemplateResponse(request, "health.html", data)
