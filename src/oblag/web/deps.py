from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from oblag import auth
from oblag.db.models import Org, User
from oblag.db.session import get_session_factory


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@dataclass
class Context:
    """Per-request auth/tenancy state. In single-org mode (auth disabled) the user
    is None and the org is the auto-provisioned default; the operator is admin."""

    user: User | None
    org: Org | None
    is_admin: bool
    csrf_token: str
    raw_session: str | None
    auth_on: bool = False
    role: str | None = None  # the user's role in the active org (owner|admin|member)

    @property
    def authed(self) -> bool:
        return self.user is not None

    @property
    def can_admin_org(self) -> bool:
        """Manage keys, invites, members — org owners/admins (or instance admins)."""
        return self.is_admin or self.role in ("owner", "admin")

    @property
    def user_email(self) -> str | None:
        return self.user.email if self.user else None


ADMIN_COOKIE = "oblag_admin"


def admin_gate_token() -> str | None:
    """The secret that locks single-org admin writes. Prefer an explicit
    OBLAG_ADMIN_TOKEN, but fall back to OBLAG_CRON_SECRET so that any real
    deployment (which must set a cron secret for scheduled fetches) is locked
    automatically — no separate dashboard step needed. Only truly open when
    neither is set (local/private use)."""
    from oblag.config import get_settings

    settings = get_settings()
    return settings.admin_token or settings.cron_secret


def _single_org_admin(request: Request) -> tuple[bool, str]:
    """Admin status + CSRF token for single-org mode. If a gate token is configured
    (OBLAG_ADMIN_TOKEN, or OBLAG_CRON_SECRET as a fallback), a request is admin only
    when it presents that token (an `oblag_admin` cookie set via /admin/unlock, or an
    X-Admin-Token header) — this locks the shared-data writes (assert-date) on a public
    deployment. Neither set (the default) keeps the open, convenient behavior for
    private/local use. CSRF is derived from the token so admin-write forms can be
    validated without a server session."""
    import hashlib

    token = admin_gate_token()
    if not token:
        return True, ""  # open mode (unchanged); no CSRF (no cookie to protect)
    presented = request.cookies.get(ADMIN_COOKIE) or request.headers.get("x-admin-token", "")
    import secrets as _secrets

    is_admin = bool(presented) and _secrets.compare_digest(presented, token)
    csrf = hashlib.sha256(f"csrf:{token}".encode()).hexdigest() if is_admin else ""
    return is_admin, csrf


def get_context(request: Request, db: Session = Depends(get_db)) -> Context:
    if not auth.auth_enabled():
        org = auth.get_default_org(db)
        is_admin, csrf = _single_org_admin(request)
        ctx = Context(
            user=None, org=org, is_admin=is_admin, csrf_token=csrf, raw_session=None, auth_on=False
        )
    else:
        bearer = request.headers.get("authorization", "")
        raw_key = bearer[7:] if bearer.lower().startswith("bearer ") else ""
        key = auth.resolve_api_key(db, raw_key) if raw_key else None
        if key is not None:
            if not auth.within_rate_limit(db, key):
                raise HTTPException(429, "rate limit exceeded")
            key_org = db.get(Org, key.org_id)
            ctx = Context(
                user=None,
                org=key_org,
                is_admin=False,
                csrf_token="",
                raw_session=None,
                auth_on=True,
            )
            request.state.ctx = ctx
            return ctx
        raw = request.cookies.get(auth.SESSION_COOKIE)
        sess = auth.resolve_session(db, raw)
        if sess is None:
            ctx = Context(
                user=None, org=None, is_admin=False, csrf_token="", raw_session=None, auth_on=True
            )
        else:
            user = db.get(User, sess.user_id)
            active_org = db.get(Org, sess.org_id) if sess.org_id else None
            role = (
                auth.member_role(db, active_org.id, user.id)
                if active_org is not None and user is not None
                else None
            )
            ctx = Context(
                user=user,
                org=active_org,
                is_admin=auth.is_instance_admin(user.email if user else None),
                csrf_token=sess.csrf_token,
                raw_session=raw,
                auth_on=True,
                role=role,
            )
    request.state.ctx = ctx  # base.html reads this for the nav
    return ctx


def login_redirect(ctx: Context):
    """In magic-link mode, redirect an unauthenticated visitor to sign-in (or a
    logged-in user without an org to onboarding). None in single-org mode."""
    from fastapi.responses import RedirectResponse

    if not ctx.auth_on:
        return None
    if not ctx.authed:
        return RedirectResponse("/auth/login", status_code=303)
    if ctx.org is None:
        return RedirectResponse("/auth/onboarding", status_code=303)
    return None


def check_csrf(ctx: Context, submitted: str | None) -> None:
    """Reject a state-changing form POST whose CSRF token doesn't match the session.
    No-op only in fully-open single-org mode (no admin token, so no cookie to protect);
    once an admin token gate is active ctx.csrf_token is set and is enforced."""
    if not auth.auth_enabled() and not ctx.csrf_token:
        return
    import secrets as _secrets

    if (
        not ctx.csrf_token
        or not submitted
        or not _secrets.compare_digest(ctx.csrf_token, submitted)
    ):
        raise HTTPException(403, "invalid or missing CSRF token")
