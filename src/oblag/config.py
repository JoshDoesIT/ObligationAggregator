from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OBLAG_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/oblag.db"
    data_dir: Path = Path("data")

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

    # Provenance (M3): path to an Ed25519 private key (PEM). Generated via `oblag keygen`.
    signing_key_path: Path | None = None

    # Scope boundary: include pre-rule/ANPRM weak signals? (spec 00 — default off)
    include_prerule: bool = False

    # OEIL watched procedure references, csv (e.g. "2021/0106(COD),2020/0359(COD)")
    oeil_procedures: str = ""

    # LegiScan (US state laws): monitored states csv (e.g. "CA,NY,TX") + search query
    legiscan_states: str = ""
    legiscan_query: str = "comprehensive data privacy"

    # EU Have Your Say topics to monitor (csv of portal topic codes)
    hys_topics: str = "DIGITAL"

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
