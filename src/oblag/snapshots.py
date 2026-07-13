from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from oblag.db.models import Snapshot

_KEPT_HEADERS = {"etag", "last-modified", "content-type"}


class SnapshotStore:
    """Content-addressed store: data/snapshots/<sha[:2]>/<sha>. Dedupes by digest."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

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
        snap = Snapshot(
            sha256=sha,
            source_url=source_url,
            adapter=adapter,
            fetched_at=fetched_at or datetime.now(UTC),
            http_status=http_status,
            http_headers=headers or None,
            storage_ref=ref,
        )
        session.add(snap)
        session.flush()
        return snap
