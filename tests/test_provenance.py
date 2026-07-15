from __future__ import annotations

import base64
import json

import pytest
from cryptography.exceptions import InvalidSignature

from oblag.provenance import Signer, generate_keypair, verify_envelope
from oblag.snapshots import SnapshotStore


@pytest.fixture()
def keypair(tmp_path):
    priv = tmp_path / "keys" / "signing.pem"
    pub = generate_keypair(priv)
    return priv, pub


def test_sign_and_verify_roundtrip(keypair):
    priv, pub = keypair
    signer = Signer.load(priv)
    assert signer is not None
    statement = signer.build_statement(
        sha256="ab" * 32,
        source_url="https://www.federalregister.gov/api/v1/documents.json",
        adapter="federal_register",
        fetch_meta={"fetched_at": "2026-07-14T00:00:00+00:00", "http_status": 200},
    )
    envelope = signer.sign_statement(statement)
    decoded = verify_envelope(envelope, pub.read_bytes())
    assert decoded == statement
    assert decoded["subject"][0]["digest"]["sha256"] == "ab" * 32
    assert decoded["predicate"]["adapter"] == "federal_register"


def test_tampered_payload_fails_verification(keypair):
    priv, pub = keypair
    signer = Signer.load(priv)
    envelope = signer.sign_statement(
        signer.build_statement(sha256="ab" * 32, source_url="https://x", adapter="a", fetch_meta={})
    )
    payload = json.loads(base64.standard_b64decode(envelope["payload"]))
    payload["subject"][0]["digest"]["sha256"] = "cd" * 32  # forge the digest
    envelope["payload"] = base64.standard_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    with pytest.raises((InvalidSignature, ValueError)):
        verify_envelope(envelope, pub.read_bytes())


def test_wrong_key_fails(keypair, tmp_path):
    priv, _pub = keypair
    other_pub = generate_keypair(tmp_path / "other.pem")
    signer = Signer.load(priv)
    envelope = signer.sign_statement(
        signer.build_statement(sha256="ab" * 32, source_url="https://x", adapter="a", fetch_meta={})
    )
    with pytest.raises((InvalidSignature, ValueError)):
        verify_envelope(envelope, other_pub.read_bytes())


def test_snapshot_store_attests_when_signer_present(db, tmp_path, keypair):
    priv, pub = keypair
    store = SnapshotStore(tmp_path / "snaps", signer=Signer.load(priv))
    snap = store.record(
        db, content=b"regulatory content", source_url="https://example.gov/doc", adapter="test"
    )
    assert snap.attestation_ref is not None
    envelope = json.loads((store.root / snap.attestation_ref).read_text())
    statement = verify_envelope(envelope, pub.read_bytes())
    assert statement["subject"][0]["digest"]["sha256"] == snap.sha256

    # unsigned store leaves attestation_ref NULL
    store2 = SnapshotStore(tmp_path / "snaps2")
    snap2 = store2.record(db, content=b"other", source_url="https://x", adapter="test")
    assert snap2.attestation_ref is None
