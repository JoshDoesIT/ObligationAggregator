from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx

from oblag.db.models import Confidence, DateType

USER_AGENT = "ObligationAggregator/0.1 (+https://github.com/JoshDoesIT/ObligationAggregator)"


@dataclass
class RawDocument:
    """One raw payload fetched from a source; stored verbatim in the snapshot store."""

    url: str
    content: bytes
    content_type: str = "application/json"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    http_status: int | None = None
    http_headers: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedDate:
    date_type: DateType
    value: date
    confidence: Confidence
    label: str | None = None


@dataclass
class NormalizedItem:
    source_system: str
    external_key: tuple[str, str]  # identity join key, e.g. ("fr_doc_number", "2024-06526")
    jurisdiction: str
    title: str
    native_status: str
    url: str | None = None
    abstract: str | None = None
    join_keys: list[tuple[str, str]] = field(default_factory=list)
    dates: list[NormalizedDate] = field(default_factory=list)
    obligation_slug: str | None = None
    track: str = "default"  # lifecycle track: "proposed" | "final" | "default" (spec 01)
    native_meta: dict[str, str] = field(default_factory=dict)  # extra statemap inputs
    anomalies: list[str] = field(default_factory=list)  # defensive-parse notes → anomaly events
    # Supplementary documents (comment extensions, corrections, delays, withdrawals)
    # are allowed to merge into an item with a DIFFERENT external key of the same type
    # — they update the root rulemaking. Root documents never are (spec 03): two root
    # docs sharing only an umbrella join key (FAA's RIN 2120-AA64 covers every
    # airworthiness directive; NOAA inseason actions share a fisheries docket) are
    # distinct rulemakings, observed merged live before this flag existed.
    supplementary: bool = False
    # Date types the source EXPLICITLY no longer states (e.g. NIST's "No Due Date:
    # Comment Period Remains Open"). The reducer retracts any live assertion of these
    # types — append-only, so the withdrawn value stays in the history. Only emit a
    # retraction on an explicit source signal, never on mere absence of a field.
    retract_dates: list[DateType] = field(default_factory=list)

    @property
    def all_join_keys(self) -> list[tuple[str, str]]:
        keys = [self.external_key]
        keys.extend(k for k in self.join_keys if k != self.external_key)
        return keys

    @property
    def content_fingerprint(self) -> str:
        """Hash of semantic content — stable across feed reordering (spec 02)."""
        payload = {
            "title": self.title,
            "abstract": self.abstract,
            "native_status": self.native_status,
            "native_meta": sorted(self.native_meta.items()),
            "dates": sorted(
                (d.date_type.value, d.label or "", d.value.isoformat(), d.confidence.value)
                for d in self.dates
            ),
            "join_keys": sorted(self.all_join_keys),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class FetchContext:
    client: httpx.Client
    since: datetime | None = None  # incremental: fetch changes after this instant
    window: tuple[date, date] | None = None  # bounded backfill window
    params: dict[str, Any] = field(default_factory=dict)  # adapter-specific knobs


class SourceAdapter(ABC):
    name: str
    jurisdiction: str

    @abstractmethod
    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        """Fetch raw payloads. May paginate. The runner snapshots each RawDocument."""

    @abstractmethod
    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        """Pure raw → items. No network access. Must not raise on malformed records:
        skip the record and note it via an item anomaly or omit it entirely."""

    def enabled(self) -> bool:
        """Adapters requiring credentials override this to self-disable when unset."""
        return True


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    )
