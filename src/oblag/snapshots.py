from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from oblag.db.models import Snapshot

_KEPT_HEADERS = {"etag", "last-modified", "content-type", "x-oblag-rendered"}


class SnapshotStore:
    """Content-addressed store: data/snapshots/<sha[:2]>/<sha>. Dedupes by digest.

    When a Signer is provided, each new snapshot gets a DSSE/in-toto attestation at
    attestations/<sha>.dsse.json (spec 04); hash-only otherwise."""

    def __init__(self, root: Path, signer=None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.signer = signer

    @classmethod
    def from_settings(cls) -> SnapshotStore:
        from oblag.config import get_settings
        from oblag.provenance import Signer

        settings = get_settings()
        key_path = settings.signing_key_path or settings.data_dir / "keys" / "signing.pem"
        return cls(settings.snapshot_dir, signer=Signer.load(key_path))

    def path_for(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256

    def write(self, content: bytes) -> tuple[str, str]:
        """Store content, return (sha256, storage_ref). Idempotent."""
        sha = hashlib.sha256(content).hexdigest()
        path = self.path_for(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(content)
            tmp.rename(path)
        return sha, str(path.relative_to(self.root))

    def read(self, sha256: str) -> bytes:
        return self.path_for(sha256).read_bytes()

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
        import json

        statement = self.signer.build_statement(
            sha256=sha, source_url=source_url, adapter=adapter, fetch_meta=fetch_meta
        )
        envelope = self.signer.sign_statement(statement)
        att_dir = self.root / "attestations"
        att_dir.mkdir(parents=True, exist_ok=True)
        path = att_dir / f"{sha}.dsse.json"
        path.write_text(json.dumps(envelope, indent=1))
        return str(path.relative_to(self.root))
