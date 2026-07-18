from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from oblag.db.models import Snapshot
from oblag.storage import LocalBackend, StorageBackend

_KEPT_HEADERS = {"etag", "last-modified", "content-type", "x-oblag-rendered"}


class SnapshotStore:
    """Content-addressed snapshot store over a pluggable backend (spec 04).

    Logical layout: snapshots/<sha[:2]>/<sha> and attestations/<sha>.dsse.json.
    With a Signer, each new snapshot gets a DSSE/in-toto attestation; hash-only
    otherwise. `storage_ref`/`attestation_ref` persist whatever the backend returns
    (relative path locally, blob URL on Vercel)."""

    def __init__(self, root: Path | StorageBackend, signer=None):
        # Path accepted for compatibility: tests and self-host call sites pass a
        # directory; production resolves the backend from settings.
        if isinstance(root, StorageBackend):
            self.backend = root
            self.root = getattr(root, "root", None)
        else:
            self.backend = LocalBackend(root)
            self.root = root
        self.signer = signer

    @classmethod
    def from_settings(cls) -> SnapshotStore:
        from oblag.provenance import Signer
        from oblag.storage import backend_from_settings

        return cls(backend_from_settings(), signer=Signer.from_settings())

    def _snapshot_path(self, sha256: str) -> str:
        return f"{sha256[:2]}/{sha256}"

    def write(self, content: bytes) -> tuple[str, str]:
        """Store content, return (sha256, storage_ref). Idempotent by digest."""
        sha = hashlib.sha256(content).hexdigest()
        ref = self.backend.write(self._snapshot_path(sha), content)
        return sha, ref

    def read_ref(self, ref: str) -> bytes:
        return self.backend.read(ref)

    def read(self, sha256: str) -> bytes:
        """Read by digest (local layout); prefer read_ref(snapshot.storage_ref)."""
        return self.backend.read(self._snapshot_path(sha256))

    def record(
        self,
        session: Session,
        *,
        content: bytes,
        source_url: str,
        adapter: str,
        http_status: int | None = None,
        http_headers: dict[str, str] | None = None,
        fetched_at: datetime | None = None,
    ) -> Snapshot:
        """Store content and persist (or return existing) Snapshot row for this digest."""
        sha, ref = self.write(content)
        existing = session.query(Snapshot).filter_by(sha256=sha).one_or_none()
        if existing is not None:
            return existing
        headers = {
            k.lower(): v for k, v in (http_headers or {}).items() if k.lower() in _KEPT_HEADERS
        }
        when = fetched_at or datetime.now(UTC)
        snap = Snapshot(
            sha256=sha,
            source_url=source_url,
            adapter=adapter,
            fetched_at=when,
            http_status=http_status,
            http_headers=headers or None,
            storage_ref=ref,
        )
        if self.signer is not None:
            snap.attestation_ref = self._attest(
                sha,
                source_url=source_url,
                adapter=adapter,
                fetch_meta={
                    "fetched_at": when.isoformat(),
                    "http_status": http_status,
                    "http_headers": headers,
                },
            )
        session.add(snap)
        session.flush()
        return snap

    def _attest(self, sha: str, *, source_url: str, adapter: str, fetch_meta: dict) -> str:
        statement = self.signer.build_statement(
            sha256=sha, source_url=source_url, adapter=adapter, fetch_meta=fetch_meta
        )
        envelope = self.signer.sign_statement(statement)
        return self.backend.write(
            f"attestations/{sha}.dsse.json",
            json.dumps(envelope, indent=1).encode(),
            content_type="application/json",
        )
