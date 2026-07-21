"""Authentication and tenancy primitives (spec 07).

Self-rolled magic-link auth: hashed single-use login tokens delivered over the
instance SMTP, exchanged for a session cookie. No passwords, no third-party
identity dependency. Single-org deployments (OBLAG_AUTH=disabled) never touch
any of this — every request is pinned to the default org.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from sqlalchemy.orm import Session

from oblag.config import get_settings
from oblag.db.models import (
    ApiKey,
    Invite,
    LoginToken,
    Org,
    OrgMember,
    User,
    UserSession,
    utcnow,
)

SESSION_COOKIE = "oblag_session"
API_KEY_PREFIX = "oblag_"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def auth_enabled() -> bool:
    return get_settings().auth == "magic-link"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _expired(dt: datetime) -> bool:
    # SQLite drops tzinfo (Postgres timestamptz keeps it); normalize before comparing.
    aware = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return aware < datetime.now(UTC)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email)) and len(email) <= 320


# --- default org (single-org mode + adoption target) -------------------------


def get_default_org(session: Session) -> Org:
    org = session.query(Org).filter_by(slug="default").one_or_none()
    if org is None:
        org = Org(slug="default", name="Default")
        session.add(org)
        session.flush()
    return org


def is_instance_admin(email: str | None) -> bool:
    if not email:
        return False
    admins = {e.strip().lower() for e in get_settings().instance_admins.split(",") if e.strip()}
    return normalize_email(email) in admins


# --- magic-link login --------------------------------------------------------


def request_login(session: Session, email: str) -> str:
    """Create a single-use login token and return the RAW token (for the email URL).
    Only the hash is persisted."""
    raw = secrets.token_urlsafe(32)
    ttl = get_settings().login_token_ttl_minutes
    session.add(
        LoginToken(
            email=normalize_email(email),
            token_hash=_hash(raw),
            expires_at=utcnow() + timedelta(minutes=ttl),
        )
    )
    session.flush()
    return raw


def verify_login(session: Session, raw_token: str) -> User | None:
    """Validate + consume a login token; get-or-create the user. None if invalid."""
    if not raw_token:
        return None
    token = session.query(LoginToken).filter_by(token_hash=_hash(raw_token)).one_or_none()
    if token is None or token.consumed_at is not None or _expired(token.expires_at):
        return None
    token.consumed_at = utcnow()
    user = session.query(User).filter_by(email=token.email).one_or_none()
    if user is None:
        user = User(email=token.email)
        session.add(user)
        session.flush()
    user.last_login_at = utcnow()
    session.flush()
    return user


# --- sessions ----------------------------------------------------------------


@dataclass
class SessionInfo:
    raw_token: str
    csrf_token: str


def create_session(session: Session, user: User, org: Org | None) -> SessionInfo:
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    ttl = get_settings().session_ttl_days
    session.add(
        UserSession(
            token_hash=_hash(raw),
            user_id=user.id,
            org_id=org.id if org else None,
            csrf_token=csrf,
            expires_at=utcnow() + timedelta(days=ttl),
        )
    )
    session.flush()
    return SessionInfo(raw_token=raw, csrf_token=csrf)


def resolve_session(session: Session, raw_token: str | None) -> UserSession | None:
    if not raw_token:
        return None
    row = session.query(UserSession).filter_by(token_hash=_hash(raw_token)).one_or_none()
    if row is None or _expired(row.expires_at):
        return None
    return row


def destroy_session(session: Session, raw_token: str | None) -> None:
    if not raw_token:
        return
    session.query(UserSession).filter_by(token_hash=_hash(raw_token)).delete()


# --- org membership ----------------------------------------------------------


def member_role(session: Session, org_id: int, user_id: int) -> str | None:
    row = session.query(OrgMember.role).filter_by(org_id=org_id, user_id=user_id).one_or_none()
    return row[0] if row else None


def user_orgs(session: Session, user_id: int) -> list[Org]:
    return (
        session.query(Org)
        .join(OrgMember, OrgMember.org_id == Org.id)
        .filter(OrgMember.user_id == user_id)
        .order_by(Org.name)
        .all()
    )


def create_org(session: Session, user: User, name: str) -> Org:
    """Create an org and make the user its owner."""
    slug = _unique_slug(session, name)
    org = Org(slug=slug, name=name.strip() or slug)
    session.add(org)
    session.flush()
    session.add(OrgMember(org_id=org.id, user_id=user.id, role="owner"))
    session.flush()
    return org


def _unique_slug(session: Session, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "org"
    slug = base
    n = 2
    while session.query(Org.id).filter_by(slug=slug).first() is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


# --- API keys (Phase 2) ------------------------------------------------------


def create_api_key(session: Session, org: Org, name: str) -> tuple[ApiKey, str]:
    """Create an org API key. Returns (row, RAW key) — the raw key is shown once."""
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    key = ApiKey(
        org_id=org.id,
        name=name.strip() or "api key",
        prefix=raw[:12],
        key_hash=_hash(raw),
    )
    session.add(key)
    session.flush()
    return key, raw


def resolve_api_key(session: Session, raw: str | None) -> ApiKey | None:
    """Active, non-revoked API key for a raw bearer token, or None."""
    if not raw or not raw.startswith(API_KEY_PREFIX):
        return None
    key = session.query(ApiKey).filter_by(key_hash=_hash(raw)).one_or_none()
    if key is None or key.revoked_at is not None:
        return None
    key.last_used_at = utcnow()
    return key


def revoke_api_key(session: Session, org: Org, key_id: int) -> bool:
    key = session.get(ApiKey, key_id)
    if key is None or key.org_id != org.id or key.revoked_at is not None:
        return False
    key.revoked_at = utcnow()
    return True


def within_rate_limit(session: Session, key: ApiKey) -> bool:
    """Fixed-window per-minute limiter. Mutates the key's counter; returns False when
    the window is exhausted."""
    limit = get_settings().api_rate_limit_per_min
    bucket = datetime.now(UTC).replace(second=0, microsecond=0)
    start = key.rl_window_start
    start = start.replace(tzinfo=UTC) if start is not None and start.tzinfo is None else start
    if start is None or start != bucket:
        key.rl_window_start = bucket
        key.rl_count = 1
        return True
    key.rl_count += 1
    return key.rl_count <= limit


# --- org invites (Phase 2) ---------------------------------------------------


def create_invite(session: Session, org: Org, email: str, role: str = "member") -> Invite:
    addr = normalize_email(email)
    inv = session.query(Invite).filter_by(org_id=org.id, email=addr).one_or_none()
    if inv is None:
        inv = Invite(org_id=org.id, email=addr, role=role if role in _ROLES else "member")
        session.add(inv)
        session.flush()
    elif inv.accepted_at is None:
        inv.role = role if role in _ROLES else inv.role
    return inv


def accept_pending_invites(session: Session, user: User) -> list[Org]:
    """On login, turn any pending invites for the user's email into memberships."""
    joined: list[Org] = []
    invites = (
        session.query(Invite).filter(Invite.email == user.email, Invite.accepted_at.is_(None)).all()
    )
    for inv in invites:
        exists = (
            session.query(OrgMember).filter_by(org_id=inv.org_id, user_id=user.id).one_or_none()
        )
        if exists is None:
            session.add(OrgMember(org_id=inv.org_id, user_id=user.id, role=inv.role))
        inv.accepted_at = utcnow()
        org = session.get(Org, inv.org_id)
        if org is not None:
            joined.append(org)
    session.flush()
    return joined


_ROLES = ("owner", "admin", "member")


# --- email -------------------------------------------------------------------


def send_login_email(to: str, verify_url: str) -> None:
    settings = get_settings()
    if not settings.smtp_host:
        raise RuntimeError("SMTP is not configured (OBLAG_SMTP_HOST) — magic-link login needs it")
    msg = EmailMessage()
    msg["Subject"] = "Your ObligationAggregator sign-in link"
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg.set_content(
        "Sign in to ObligationAggregator by opening this link "
        f"(valid {settings.login_token_ttl_minutes} minutes, single use):\n\n{verify_url}\n\n"
        "If you didn't request this, you can ignore this email."
    )
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        if settings.smtp_user and settings.smtp_password:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
