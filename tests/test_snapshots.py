from __future__ import annotations

import hashlib

from oblag.snapshots import SnapshotStore


def test_write_is_content_addressed_and_idempotent(snapshot_root):
    store = SnapshotStore(snapshot_root)
    sha1, ref1 = store.write(b"hello world")
    sha2, ref2 = store.write(b"hello world")
    assert sha1 == sha2 == hashlib.sha256(b"hello world").hexdigest()
    assert ref1 == ref2
    assert store.read(sha1) == b"hello world"
    assert (snapshot_root / sha1[:2] / sha1).exists()


def test_record_dedupes_by_digest(db, snapshot_root):
    store = SnapshotStore(snapshot_root)
    s1 = store.record(
        db,
        content=b"payload",
        source_url="https://example.gov/a",
        adapter="test",
        http_status=200,
        http_headers={"ETag": "abc", "X-Ignore-Me": "1", "Last-Modified": "yesterday"},
    )
    s2 = store.record(db, content=b"payload", source_url="https://example.gov/b", adapter="test")
    assert s1.id == s2.id
    assert s1.http_headers == {"etag": "abc", "last-modified": "yesterday"}


def test_fingerprint_stable_across_date_order():
    from datetime import date

    from oblag.adapters.base import NormalizedDate, NormalizedItem
    from oblag.db.models import Confidence, DateType

    d1 = NormalizedDate(DateType.comment_close, date(2024, 6, 3), Confidence.published_firm)
    d2 = NormalizedDate(DateType.effective, date(2025, 1, 1), Confidence.published_firm)
    a = NormalizedItem(
        source_system="s",
        external_key=("k", "1"),
        jurisdiction="US",
        title="t",
        native_status="PRORULE",
        dates=[d1, d2],
    )
    b = NormalizedItem(
        source_system="s",
        external_key=("k", "1"),
        jurisdiction="US",
        title="t",
        native_status="PRORULE",
        dates=[d2, d1],
    )
    assert a.content_fingerprint == b.content_fingerprint
