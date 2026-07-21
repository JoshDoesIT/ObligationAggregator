"""Magic-link auth routes (spec 07). Mounted only conceptually — the routes exist
in all modes but are inert when OBLAG_AUTH=disabled (login pages redirect home)."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from oblag import auth
from oblag.config import get_settings
from oblag.db.models import LoginToken, utcnow
from oblag.web.deps import Context, check_csrf, get_context, get_db

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
log = logging.getLogger(__name__)

_MAX_LOGIN_REQUESTS_PER_HOUR = 5


def _set_session_cookie(resp: RedirectResponse, raw_token: str) -> None:
    settings = get_settings()
    resp.set_cookie(
        auth.SESSION_COOKIE,
        raw_token,
        max_age=settings.session_ttl_days * 86400,
        httponly=True,
        secure=settings.base_url.startswith("https"),
        samesite="lax",
    )


@router.get("/auth/login", response_class=HTMLResponse)
def login_form(request: Request, ctx: Context = Depends(get_context)):
    if not auth.auth_enabled() or ctx.authed:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"ctx": ctx, "sent": False})


@router.post("/auth/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(""),
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    if not auth.auth_enabled():
        return RedirectResponse("/", status_code=303)
    addr = auth.normalize_email(email)
    # Always render the same "check your email" page — no account enumeration.
    if auth.valid_email(addr):
        recent = (
            db.query(LoginToken)
            .filter(
                LoginToken.email == addr, LoginToken.created_at >= utcnow() - timedelta(hours=1)
            )
            .count()
        )
        if recent < _MAX_LOGIN_REQUESTS_PER_HOUR:
            raw = auth.request_login(db, addr)
            url = f"{get_settings().base_url.rstrip('/')}/auth/verify?token={raw}"
            try:
                auth.send_login_email(addr, url)
            except Exception as exc:  # noqa: BLE001 — surface config problem in logs only
                log.warning("magic-link email failed for %s: %s", addr, exc)
    return templates.TemplateResponse(request, "login.html", {"ctx": ctx, "sent": True})


@router.get("/auth/verify", response_class=HTMLResponse)
def verify(request: Request, token: str = "", db: Session = Depends(get_db)):
    if not auth.auth_enabled():
        return RedirectResponse("/", status_code=303)
    user = auth.verify_login(db, token)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"ctx": None, "sent": False, "error": "That link is invalid or expired."},
        )
    auth.accept_pending_invites(db, user)  # join any orgs that invited this email
    orgs = auth.user_orgs(db, user.id)
    active = orgs[0] if orgs else None
    info = auth.create_session(db, user, active)
    dest = "/watchlists" if active else "/auth/onboarding"
    resp = RedirectResponse(dest, status_code=303)
    _set_session_cookie(resp, info.raw_token)
    return resp


@router.post("/auth/logout")
def logout(
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    check_csrf(ctx, csrf_token)
    auth.destroy_session(db, ctx.raw_session)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@router.get("/auth/onboarding", response_class=HTMLResponse)
def onboarding_form(request: Request, ctx: Context = Depends(get_context)):
    if not auth.auth_enabled() or not ctx.authed:
        return RedirectResponse("/auth/login", status_code=303)
    if ctx.org is not None:
        return RedirectResponse("/watchlists", status_code=303)
    return templates.TemplateResponse(request, "onboarding.html", {"ctx": ctx})


@router.post("/auth/onboarding")
def onboarding_submit(
    org_name: str = Form(""),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    if not auth.auth_enabled() or ctx.user is None:
        return RedirectResponse("/auth/login", status_code=303)
    check_csrf(ctx, csrf_token)
    org = auth.create_org(db, ctx.user, org_name.strip() or f"{ctx.user_email}'s org")
    # point the current session at the new org
    sess = auth.resolve_session(db, ctx.raw_session)
    if sess is not None:
        sess.org_id = org.id
    return RedirectResponse("/watchlists", status_code=303)


def _settings_data(db: Session, ctx: Context) -> dict:
    from oblag.db.models import ApiKey, Invite, OrgMember, User

    assert ctx.org is not None  # callers guard on ctx.org before invoking
    org_id = ctx.org.id
    keys = (
        db.query(ApiKey).filter_by(org_id=org_id, revoked_at=None).order_by(ApiKey.id.desc()).all()
    )
    invites = (
        db.query(Invite).filter_by(org_id=org_id, accepted_at=None).order_by(Invite.email).all()
    )
    members = [
        {"email": email, "role": role}
        for email, role in db.query(User.email, OrgMember.role)
        .join(OrgMember, OrgMember.user_id == User.id)
        .filter(OrgMember.org_id == org_id)
        .order_by(User.email)
    ]
    return {"ctx": ctx, "keys": keys, "invites": invites, "members": members}


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, db: Session = Depends(get_db), ctx: Context = Depends(get_context)
):
    if not auth.auth_enabled() or ctx.org is None:
        return RedirectResponse("/auth/login", status_code=303)
    data = _settings_data(db, ctx)
    data["new_key"] = None
    return templates.TemplateResponse(request, "settings.html", data)


@router.post("/settings/api-keys", response_class=HTMLResponse)
def create_api_key(
    request: Request,
    name: str = Form("api key"),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    if not auth.auth_enabled() or ctx.org is None:
        return RedirectResponse("/auth/login", status_code=303)
    check_csrf(ctx, csrf_token)
    if not ctx.can_admin_org:
        return RedirectResponse("/settings", status_code=303)
    try:
        auth.enforce_quota(db, ctx.org.id, "api_keys")
    except auth.QuotaError as exc:
        data = _settings_data(db, ctx)
        data["new_key"] = None
        data["error"] = str(exc)
        return templates.TemplateResponse(request, "settings.html", data)
    _key, raw = auth.create_api_key(db, ctx.org, name)
    data = _settings_data(db, ctx)
    data["new_key"] = raw  # shown exactly once
    return templates.TemplateResponse(request, "settings.html", data)


@router.post("/settings/api-keys/{key_id}/revoke")
def revoke_api_key(
    key_id: int,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    if not auth.auth_enabled() or ctx.org is None:
        return RedirectResponse("/auth/login", status_code=303)
    check_csrf(ctx, csrf_token)
    if ctx.can_admin_org:
        auth.revoke_api_key(db, ctx.org, key_id)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/email")
def update_email_prefs(
    notify_from_name: str = Form(""),
    notify_reply_to: str = Form(""),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    if not auth.auth_enabled() or ctx.org is None:
        return RedirectResponse("/auth/login", status_code=303)
    check_csrf(ctx, csrf_token)
    if ctx.can_admin_org:
        from oblag.db.models import Org

        org = db.get(Org, ctx.org.id)
        if org is not None:
            org.notify_from_name = notify_from_name.strip() or None
            reply = auth.normalize_email(notify_reply_to)
            org.notify_reply_to = reply if auth.valid_email(reply) else None
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/invites")
def invite_member(
    email: str = Form(""),
    role: str = Form("member"),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    if not auth.auth_enabled() or ctx.org is None:
        return RedirectResponse("/auth/login", status_code=303)
    check_csrf(ctx, csrf_token)
    if ctx.can_admin_org and auth.valid_email(auth.normalize_email(email)):
        try:
            auth.enforce_quota(db, ctx.org.id, "invites")
            auth.create_invite(db, ctx.org, email, role)
        except auth.QuotaError:
            pass  # silently ignore over-quota invite; settings page shows the cap
    return RedirectResponse("/settings", status_code=303)


@router.post("/auth/switch-org")
def switch_org(
    org_id: int = Form(...),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    if not auth.auth_enabled() or ctx.user is None:
        return RedirectResponse("/auth/login", status_code=303)
    check_csrf(ctx, csrf_token)
    # only switch to an org the user actually belongs to
    if any(o.id == org_id for o in auth.user_orgs(db, ctx.user.id)):
        sess = auth.resolve_session(db, ctx.raw_session)
        if sess is not None:
            sess.org_id = org_id
    return RedirectResponse("/watchlists", status_code=303)
