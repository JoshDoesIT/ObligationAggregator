from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PAYLOAD_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://github.com/JoshDoesIT/ObligationAggregator/attestations/fetch/v1"


def generate_keypair(private_path: Path) -> Path:
    """Write an Ed25519 private key (PEM) + public key beside it. Returns pub path."""
    private_path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    private_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    pub_path = private_path.with_suffix(".pub")
    pub_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return pub_path


def _key_id(public_key: Ed25519PublicKey) -> str:
    der = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(der).hexdigest()


def _pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE Pre-Authentication Encoding."""
    return b"DSSEv1 %d %s %d %s" % (
        len(payload_type),
        payload_type.encode(),
        len(payload),
        payload,
    )


class Signer:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._key = private_key
        self.key_id = _key_id(private_key.public_key())

    @classmethod
    def load(cls, path: Path) -> Signer | None:
        if not path.exists():
            return None
        return cls.from_pem(path.read_bytes())

    @classmethod
    def from_pem(cls, pem: bytes) -> Signer:
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("not an Ed25519 private key")
        return cls(key)

    @classmethod
    def from_settings(cls) -> Signer | None:
        """Env-var PEM takes precedence (serverless); falls back to the key file."""
        from oblag.config import get_settings

        settings = get_settings()
        if settings.signing_key_pem:
            return cls.from_pem(settings.signing_key_pem.encode())
        key_path = settings.signing_key_path or settings.data_dir / "keys" / "signing.pem"
        return cls.load(key_path)

    def build_statement(
        self, *, sha256: str, source_url: str, adapter: str, fetch_meta: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "_type": STATEMENT_TYPE,
            "subject": [{"name": source_url, "digest": {"sha256": sha256}}],
            "predicateType": PREDICATE_TYPE,
            "predicate": {"adapter": adapter, **fetch_meta},
        }

    def sign_statement(self, statement: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()
        sig = self._key.sign(_pae(PAYLOAD_TYPE, payload))
        return {
            "payload": base64.standard_b64encode(payload).decode(),
            "payloadType": PAYLOAD_TYPE,
            "signatures": [{"keyid": self.key_id, "sig": base64.standard_b64encode(sig).decode()}],
        }


def verify_envelope(envelope: dict[str, Any], public_key_pem: bytes) -> dict[str, Any]:
    """Verify a DSSE envelope; returns the decoded statement. Raises on any mismatch."""
    public_key = serialization.load_pem_public_key(public_key_pem)
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("not an Ed25519 public key")
    payload = base64.standard_b64decode(envelope["payload"])
    expected_keyid = _key_id(public_key)
    for signature in envelope["signatures"]:
        if signature.get("keyid") not in (None, "", expected_keyid):
            continue
        public_key.verify(
            base64.standard_b64decode(signature["sig"]),
            _pae(envelope["payloadType"], payload),
        )
        return json.loads(payload)
    raise ValueError("no signature matched the provided public key")
