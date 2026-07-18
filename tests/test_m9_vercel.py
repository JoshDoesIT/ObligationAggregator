from __future__ import annotations

from datetime import date

import pytest
import respx
from httpx import Response

from conftest import load_fixture
from oblag.adapters.aicpa import AicpaAdapter
from oblag.adapters.base import RawDocument
from oblag.adapters.hitrust import HitrustAdapter
from oblag.core.reducer import reduce_item
from oblag.db.models import ItemState
from oblag.storage import LocalBackend, StorageError, VercelBlobBackend

# --- AICPA (sitemap) ---


def _items(adapter, *fixture):
    raw = RawDocument(url="https://test", content=load_fixture(*fixture))
    return list(adapter.normalize(raw))


def test_aicpa_exposure_drafts_from_sitemap(db):
    items = _items(AicpaAdapter(), "aicpa", "sitemap.xml")
    assert len(items) >= 8  # slug-anchored exposure-draft URLs; noise excluded
    assert all("exposure" in i.url.lower() or "exposure" in i.title.lower() for i in items)
    ethics = next(i for i in items if "529" in i.url)
    assert ethics.native_status == "exposure_draft"
    assert ethics.title.startswith("AICPA exposure draft: ")
    res = reduce_item(db, ethics, today=date(2026, 7, 18))
    assert res.item.state is ItemState.proposed

    # curated deadline assertion upgrades it into the comment-window lifecycle
    from oblag.core.assertions import assert_date
    from oblag.core.reducer import tick
    from oblag.db.models import Confidence, DateType

    assert_date(
        db,
        res.item.id,
        DateType.comment_close,
        date(2026, 8, 1),
        Confidence.published_firm,
        note="per exposure draft PDF",
    )
    events = tick(db, today=date(2026, 7, 19))
    assert [e.payload["to"] for e in events] == ["comment_open"]
    events = tick(db, today=date(2026, 8, 2))
    assert [e.payload["to"] for e in events] == ["comment_closed"]


def test_aicpa_noise_pages_excluded():
    items = _items(AicpaAdapter(), "aicpa", "sitemap.xml")
    assert not any("/category/" in i.url for i in items)


# --- HITRUST (sitemap) ---


def test_hitrust_version_releases_and_advisories(db):
    items = _items(HitrustAdapter(), "hitrust", "sitemap.xml")
    by_key = {i.external_key: i for i in items}
    assert ("hitrust_release", "11.3.0") in by_key
    v11_3 = by_key[("hitrust_release", "11.3.0")]
    assert v11_3.title == "HITRUST CSF v11.3.0"
    assert v11_3.obligation_slug == "hitrust-csf"
    res = reduce_item(db, v11_3, today=date(2026, 7, 18))
    assert res.item.state is ItemState.effective
    # version-tied advisory captured; marketing/case-study pages never ingested
    assert any(k[0] == "hitrust_advisory" for k in by_key)
    assert not any("/case-studies/" in i.url for i in items)
    # "99.41-resilience…" blog slug must NOT be parsed as CSF v99.41
    assert ("hitrust_release", "99.41") not in by_key


# --- storage backends ---


def test_local_backend_roundtrip(tmp_path):
    backend = LocalBackend(tmp_path / "store")
    ref = backend.write("aa/bb", b"content")
    assert ref == "aa/bb"
    assert backend.read(ref) == b"content"


def test_vercel_blob_backend(monkeypatch):
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_test_token")
    backend = VercelBlobBackend()
    with respx.mock() as mock:
        put = mock.put("https://blob.vercel-storage.com/oblag/ab/sha123").mock(
            return_value=Response(
                200, json={"url": "https://x.public.blob.vercel-storage.com/oblag/ab/sha123"}
            )
        )
        ref = backend.write("ab/sha123", b"snapshot bytes")
        assert ref == "https://x.public.blob.vercel-storage.com/oblag/ab/sha123"
        sent = put.calls[0].request
        assert sent.headers["authorization"] == "Bearer vercel_blob_test_token"
        assert sent.headers["x-add-random-suffix"] == "0"

        mock.get(ref).mock(return_value=Response(200, content=b"snapshot bytes"))
        assert backend.read(ref) == b"snapshot bytes"


def test_vercel_blob_requires_token(monkeypatch):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    with pytest.raises(StorageError, match="BLOB_READ_WRITE_TOKEN"):
        VercelBlobBackend()


def test_snapshot_store_over_blob_backend(db, monkeypatch):
    from oblag.snapshots import SnapshotStore

    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "t")
    backend = VercelBlobBackend()
    store = SnapshotStore(backend)
    with respx.mock() as mock:
        mock.put(url__startswith="https://blob.vercel-storage.com/oblag/").mock(
            return_value=Response(200, json={"url": "https://pub.blob/x"})
        )
        snap = store.record(
            db, content=b"payload", source_url="https://example.gov", adapter="test"
        )
    assert snap.storage_ref == "https://pub.blob/x"


# --- signing key from env ---


def test_signer_from_env_pem(monkeypatch, tmp_path):
    from oblag.provenance import Signer, generate_keypair

    priv = tmp_path / "signing.pem"
    generate_keypair(priv)
    monkeypatch.setenv("OBLAG_SIGNING_KEY_PEM", priv.read_text())
    from oblag.config import get_settings

    get_settings.cache_clear()
    signer = Signer.from_settings()
    assert signer is not None
    envelope = signer.sign_statement(
        signer.build_statement(sha256="ab" * 32, source_url="https://x", adapter="a", fetch_meta={})
    )
    assert envelope["signatures"][0]["keyid"] == signer.key_id


# --- cron endpoints ---


def test_cron_endpoints_hidden_without_secret(client):
    assert client.get("/api/internal/tick").status_code == 404


@pytest.fixture()
def cron_client(client, monkeypatch):
    monkeypatch.setenv("OBLAG_CRON_SECRET", "s3cret")
    from oblag.config import get_settings

    get_settings.cache_clear()
    yield client
    get_settings.cache_clear()


def test_cron_auth_enforced(cron_client):
    assert cron_client.get("/api/internal/tick").status_code == 401
    assert (
        cron_client.get("/api/internal/tick", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    r = cron_client.get("/api/internal/tick", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert "transitions" in r.json()


def test_cron_run_group_skips_disabled(cron_client, monkeypatch):
    # regulations_gov/legiscan/eba etc. lack credentials in tests → reported skipped;
    # stub the runner so no adapter touches the network
    import oblag.browserfetch as bf
    import oblag.web.internal as internal

    monkeypatch.setattr(
        internal, "_run_one", lambda db, name, since_days: {"adapter": name, "items": 0}
    )
    monkeypatch.setattr(bf, "browser_available", lambda: False)  # eba self-disables
    r = cron_client.get(
        "/api/internal/run-group/weekly", headers={"Authorization": "Bearer s3cret"}
    )
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert {x["adapter"] for x in runs} == set(
        __import__("oblag.scheduler", fromlist=["ADAPTER_GROUPS"]).ADAPTER_GROUPS["weekly"]
    )
    assert any(x.get("skipped") for x in runs)  # eba (no browser) reports skipped
    assert (
        cron_client.get(
            "/api/internal/run-group/nope", headers={"Authorization": "Bearer s3cret"}
        ).status_code
        == 404
    )


# --- remote CDP browser availability ---


def test_cdp_url_reaches_render_path(monkeypatch):
    monkeypatch.setenv("OBLAG_BROWSER_CDP_URL", "wss://chrome.example/ws")
    from oblag.config import get_settings

    get_settings.cache_clear()
    import oblag.browserfetch as bf

    assert bf._cdp_url() == "wss://chrome.example/ws"
    get_settings.cache_clear()


def test_malformed_sitemap_falls_back_to_tolerant_parse():
    # AICPA's live sitemap contains a raw '&' — one bad entity must not lose the file
    bad = (
        b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://www.aicpa-cima.com/news/article/bad-a&a-slug</loc></url>"
        b"<url><loc>https://www.aicpa-cima.com/news/download/ethics-exposure-draft-x</loc>"
        b"<lastmod>2026-06-01</lastmod></url></urlset>"
    )
    items = list(AicpaAdapter().normalize(RawDocument(url="https://t", content=bad)))
    assert len(items) == 1
    assert items[0].url.endswith("ethics-exposure-draft-x")
    assert items[0].dates and items[0].dates[0].value == date(2026, 6, 1)
