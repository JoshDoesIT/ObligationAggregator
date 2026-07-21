from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_database_url() -> str:
    # Serverless filesystems are read-only except /tmp: boot against an ephemeral
    # SQLite there until OBLAG_DATABASE_URL points at Postgres (docs/deploy-vercel.md).
    if os.environ.get("VERCEL"):
        return "sqlite:////tmp/oblag/oblag.db"
    return "sqlite:///data/oblag.db"


def _default_data_dir() -> Path:
    return Path("/tmp/oblag") if os.environ.get("VERCEL") else Path("data")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OBLAG_", env_file=".env", extra="ignore")

    database_url: str = Field(default_factory=_default_database_url)
    data_dir: Path = Field(default_factory=_default_data_dir)

    # Source credentials (all optional; adapters disable themselves when unset)
    regsgov_api_key: str | None = None
    legiscan_api_key: str | None = None

    # Notifications
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "oblag@localhost"
    base_url: str = "http://localhost:8000"

    # Multi-tenancy / auth (spec 07). Default "disabled" preserves single-org
    # behavior: no login, every request pinned to the auto-provisioned default org.
    # Set "magic-link" for a hosted multi-org deployment.
    auth: str = "disabled"  # disabled | magic-link
    instance_admins: str = ""  # csv of emails granted cross-tenant/admin operations
    session_ttl_days: int = 30
    login_token_ttl_minutes: int = 15
    api_rate_limit_per_min: int = 600  # per API key, fixed-window

    # Provenance (M3): Ed25519 private key — a PEM string (serverless: set
    # OBLAG_SIGNING_KEY_PEM from `oblag keygen` output) or a file path.
    signing_key_pem: str | None = None
    signing_key_path: Path | None = None

    # Deployment (M9): "local" filesystem or "vercel-blob" object storage for
    # snapshots/attestations; cron endpoints are enabled by setting a secret
    # (Vercel injects CRON_SECRET as the Authorization bearer on cron invocations).
    storage_backend: str = "local"
    cron_secret: str | None = None

    # Browser tier: remote Chromium over CDP (e.g. wss://…browserless…?token=…) for
    # serverless platforms that cannot run a local browser.
    browser_cdp_url: str | None = None

    # Scope boundary: include pre-rule/ANPRM weak signals? (spec 00 — default off)
    include_prerule: bool = False

    # OEIL watched procedure references, csv (e.g. "2021/0106(COD),2020/0359(COD)")
    oeil_procedures: str = ""

    # LegiScan (US state laws): monitored states csv (e.g. "CA,NY,TX") + search query
    legiscan_states: str = ""
    legiscan_query: str = "comprehensive data privacy"

    # EU Have Your Say topics to monitor (csv of portal topic codes)
    hys_topics: str = "DIGITAL"

    # Relevance gate for broad sources (Federal Register, CELLAR, Have Your Say):
    # only security/privacy-scoped documents become items (oblag/scope.py).
    # Disable to ingest everything; extend the vocabulary with extra csv terms.
    scope_filter: bool = True
    scope_extra_terms: str = ""

    # AI assist (M6): entirely optional; off unless a provider is configured.
    ai_provider: str | None = None  # "anthropic" | "openai-compatible" | None
    ai_api_key: str | None = None
    ai_base_url: str | None = None
    ai_model: str = "claude-sonnet-5"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def private_dir(self) -> Path:
        return self.data_dir / "private"


@lru_cache
def get_settings() -> Settings:
    return Settings()
