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
from oblag.db.models import LoginToken, Org, OrgMember, User, UserSession, utcnow

SESSION_COOKIE = "oblag_session"
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
