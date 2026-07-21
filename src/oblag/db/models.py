from __future__ import annotations

import enum
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ItemState(enum.StrEnum):
    proposed = "proposed"
    comment_open = "comment_open"
    comment_closed = "comment_closed"
    final_pending_effective = "final_pending_effective"
    effective = "effective"
    withdrawn = "withdrawn"
    stalled = "stalled"
    superseded = "superseded"


class DateType(enum.StrEnum):
    proposal_date = "proposal_date"
    comment_open = "comment_open"
    comment_close = "comment_close"
    projected_final = "projected_final"
    adopted = "adopted"
    effective = "effective"
    phased_compliance = "phased_compliance"
    entry_into_force = "entry_into_force"
    application = "application"
    transition_deadline = "transition_deadline"


class Confidence(enum.StrEnum):
    statutory_hard = "statutory_hard"
    published_firm = "published_firm"
    agency_estimate = "agency_estimate"
    derived = "derived"


class EventType(enum.StrEnum):
    item_created = "item_created"
    state_changed = "state_changed"
    date_changed = "date_changed"
    content_changed = "content_changed"
    item_resolved = "item_resolved"
    anomaly = "anomaly"


class DisplayPolicy(enum.StrEnum):
    full_text = "full_text"
    ids_and_titles = "ids_and_titles"
    ids_only = "ids_only"
    events_only = "events_only"


class CopyrightStatus(enum.StrEnum):
    public_domain = "public_domain"
    eu_reuse = "eu_reuse"
    licensed = "licensed"
    copyrighted = "copyrighted"


class Obligation(Base):
    __tablename__ = "obligation"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    issuing_body: Mapped[str] = mapped_column(String(255))
    jurisdiction: Mapped[str] = mapped_column(String(64))
    canonical_url: Mapped[str | None] = mapped_column(Text)
    copyright_status: Mapped[CopyrightStatus] = mapped_column(
        Enum(CopyrightStatus), default=CopyrightStatus.public_domain
    )
    display_policy: Mapped[DisplayPolicy] = mapped_column(
        Enum(DisplayPolicy), default=DisplayPolicy.full_text
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    items: Mapped[list[PipelineItem]] = relationship(back_populates="obligation")


class PipelineItem(Base):
    __tablename__ = "pipeline_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_system: Mapped[str] = mapped_column(String(64), index=True)
    jurisdiction: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    state: Mapped[ItemState] = mapped_column(Enum(ItemState), index=True)
    native_status: Mapped[str | None] = mapped_column(String(255))
    track: Mapped[str] = mapped_column(String(16), default="default")
    native_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_fingerprint: Mapped[str | None] = mapped_column(String(64))
    obligation_id: Mapped[int | None] = mapped_column(ForeignKey("obligation.id"))
    resolved_change_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_item.id"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    obligation: Mapped[Obligation | None] = relationship(back_populates="items")
    join_keys: Mapped[list[JoinKey]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    key_dates: Mapped[list[KeyDate]] = relationship(
        back_populates="item", cascade="all, delete-orphan", foreign_keys="KeyDate.pipeline_item_id"
    )
    events: Mapped[list[Event]] = relationship(back_populates="item")


class JoinKey(Base):
    __tablename__ = "join_key"
    __table_args__ = (
        UniqueConstraint("pipeline_item_id", "type", "value", name="uq_join_key_item_type_value"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_item_id: Mapped[int] = mapped_column(ForeignKey("pipeline_item.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    value: Mapped[str] = mapped_column(Text)

    item: Mapped[PipelineItem] = relationship(back_populates="join_keys")


class Snapshot(Base):
    __tablename__ = "snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_url: Mapped[str] = mapped_column(Text)
    adapter: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    http_status: Mapped[int | None] = mapped_column(Integer)
    http_headers: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    storage_ref: Mapped[str] = mapped_column(Text)
    attestation_ref: Mapped[str | None] = mapped_column(Text)


class KeyDate(Base):
    """Append-only date assertions. Never UPDATE a value; supersede it (spec 00 inv. 1).

    A retraction is itself an append-only assertion: a row with retracted=True
    supersedes the previous value and means "the source no longer states this date"
    (e.g. NIST flipping a draft to 'no closing date — ongoing comment period').
    `value` keeps the withdrawn date for the audit trail."""

    __tablename__ = "key_date"

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_item_id: Mapped[int] = mapped_column(ForeignKey("pipeline_item.id"), index=True)
    date_type: Mapped[DateType] = mapped_column(Enum(DateType))
    label: Mapped[str | None] = mapped_column(String(255))
    value: Mapped[date] = mapped_column(Date)
    confidence: Mapped[Confidence] = mapped_column(Enum(Confidence))
    retracted: Mapped[bool] = mapped_column(Boolean, default=False)
    source_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("snapshot.id"))
    asserted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("key_date.id"))

    item: Mapped[PipelineItem] = relationship(
        back_populates="key_dates", foreign_keys=[pipeline_item_id]
    )


class Event(Base):
    __tablename__ = "event"

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_item_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_item.id"), index=True)
    type: Mapped[EventType] = mapped_column(Enum(EventType), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("snapshot.id"))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    item: Mapped[PipelineItem | None] = relationship(back_populates="events")


class Watchlist(Base):
    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    # tenancy (spec 07): nullable during migration; boot adopts orphans into the
    # default org. In single-org mode every watchlist belongs to the default org.
    org_id: Mapped[int | None] = mapped_column(ForeignKey("org.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str] = mapped_column(String(16))  # rss | email | webhook
    target: Mapped[str | None] = mapped_column(Text)
    # webhook HMAC secret (Phase 2): signs the X-Oblag-Signature header so the org's
    # endpoint can verify authenticity. Set only for webhook channels.
    signing_secret: Mapped[str | None] = mapped_column(String(64))
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationLog(Base):
    __tablename__ = "notification_log"
    __table_args__ = (
        UniqueConstraint("watchlist_id", "event_id", name="uq_notification_watchlist_event"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    watchlist_id: Mapped[int] = mapped_column(ForeignKey("watchlist.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id"), index=True)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(16))  # sent | failed
    detail: Mapped[str | None] = mapped_column(Text)


class AdapterHealth(Base):
    __tablename__ = "adapter_health"

    id: Mapped[int] = mapped_column(primary_key=True)
    adapter: Mapped[str] = mapped_column(String(64), unique=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    items_seen_last_run: Mapped[int] = mapped_column(Integer, default=0)


class PrivateDocument(Base):
    """BYOL store. Rows/files here must NEVER appear in shared outputs (spec 00 inv. 3)."""

    __tablename__ = "private_document"
    __table_args__ = (
        UniqueConstraint("obligation_id", "version_label", name="uq_private_doc_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    obligation_id: Mapped[int] = mapped_column(ForeignKey("obligation.id"), index=True)
    version_label: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    storage_ref: Mapped[str] = mapped_column(String(255))
    license_attested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Multi-tenancy (spec 07) -------------------------------------------------
# All additive: the pipeline data (obligations, items, events, snapshots) is
# shared and never tenant-scoped. Only these tables + watchlist.org_id carry
# tenancy. Single-org deployments run with OBLAG_AUTH=disabled and a single
# auto-provisioned default org, so these tables stay effectively invisible.


class Org(Base):
    __tablename__ = "org"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    # "user" is reserved in Postgres; the table is app_user.
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # lowercased
    display_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrgMember(Base):
    __tablename__ = "org_member"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_org_member"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("org.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="member")  # owner|admin|member
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LoginToken(Base):
    """Single-use magic-link token. Only the SHA-256 hash is stored; the raw token
    lives only in the emailed URL."""

    __tablename__ = "login_token"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSession(Base):
    """Browser session. Cookie carries the raw token; only its hash is stored, so a
    DB read can't forge sessions. Carries the active org for multi-org users."""

    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("org.id"))
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    """Programmatic access token for an org (Phase 2). Raw key shown once at creation;
    only the SHA-256 hash is stored. rl_* columns back a per-key fixed-window limiter."""

    __tablename__ = "api_key"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("org.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    prefix: Mapped[str] = mapped_column(String(16))  # display-only, e.g. "oblag_ab12"
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rl_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rl_count: Mapped[int] = mapped_column(Integer, default=0)


class Invite(Base):
    """Pending org membership keyed by email; accepted on the invitee's first login."""

    __tablename__ = "invite"
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_invite_org_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("org.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)  # lowercased
    role: Mapped[str] = mapped_column(String(16), default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
