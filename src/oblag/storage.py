"""Pluggable object storage for snapshots/attestations (spec 04).

Backends:
- local  — content-addressed files under OBLAG_DATA_DIR (self-host default;
  storage_ref = relative path, unchanged from earlier releases)
- vercel-blob — Vercel Blob over its REST API (serverless deployments have no
  persistent filesystem; storage_ref = the blob URL)

BYOL private documents intentionally stay on the local backend: they are added and
diffed via the CLI on a trusted machine and must never reach shared/public storage."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

VERCEL_BLOB_API = "https://blob.vercel-storage.com"


class StorageError(RuntimeError):
    pass


class StorageBackend(ABC):
    @abstractmethod
    def write(
        self, path: str, content: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        """Store content at a logical path; returns the storage_ref to persist."""

    @abstractmethod
    def read(self, ref: str) -> bytes:
        """Read content by a previously returned storage_ref."""


class LocalBackend(StorageBackend):
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def write(
        self, path: str, content: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        dest = self.root / path
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(content)
            tmp.rename(dest)
        return path

    def read(self, ref: str) -> bytes:
        return (self.root / ref).read_bytes()


class VercelBlobBackend(StorageBackend):
    """Vercel Blob REST API (token from BLOB_READ_WRITE_TOKEN, injected by Vercel).

    Deterministic pathnames (random suffix disabled) keep content addressing intact;
    the returned blob URL is persisted as storage_ref."""

    def __init__(self, token: str | None = None, prefix: str = "oblag"):
        self.token = token or os.environ.get("BLOB_READ_WRITE_TOKEN") or ""
        if not self.token:
            raise StorageError(
                "vercel-blob storage requires BLOB_READ_WRITE_TOKEN (create a Blob store "
                "in the Vercel dashboard and link it to the project)"
            )
        self.prefix = prefix.strip("/")
        self.api_version = os.environ.get("OBLAG_BLOB_API_VERSION", "7")
        # New Vercel Blob stores default to private access (public URLs disabled);
        # must match the store's configured mode or uploads 400.
        self.access = os.environ.get("OBLAG_BLOB_ACCESS", "private")

    def write(
        self, path: str, content: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        pathname = f"{self.prefix}/{path}"
        resp = httpx.put(
            f"{VERCEL_BLOB_API}/{pathname}",
            content=content,
            headers={
                "authorization": f"Bearer {self.token}",
                "x-api-version": self.api_version,
                "x-vercel-blob-access": self.access,
                "x-content-type": content_type,
                "x-add-random-suffix": "0",
                # snapshots are immutable; identical re-uploads may overwrite in place
                "x-allow-overwrite": "1",
            },
            timeout=60.0,
        )
        if resp.status_code >= 300:
            raise StorageError(f"blob upload failed ({resp.status_code}): {resp.text[:200]}")
        url = resp.json().get("url")
        if not url:
            raise StorageError("blob upload response had no url")
        return url

    def read(self, ref: str) -> bytes:
        # bearer auth is required for private stores and harmless for public ones
        resp = httpx.get(ref, headers={"authorization": f"Bearer {self.token}"}, timeout=60.0)
        if resp.status_code >= 300:
            raise StorageError(f"blob read failed ({resp.status_code}) for {ref}")
        return resp.content


def backend_from_settings() -> StorageBackend:
    from oblag.config import get_settings

    settings = get_settings()
    if settings.storage_backend == "vercel-blob":
        return VercelBlobBackend()
    if settings.storage_backend != "local":
        raise StorageError(f"unknown OBLAG_STORAGE_BACKEND {settings.storage_backend!r}")
    return LocalBackend(settings.snapshot_dir)  # layout unchanged from earlier releases
