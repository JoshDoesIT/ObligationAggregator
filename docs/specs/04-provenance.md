# Spec 04 — Provenance & Attestations

Every fetch is stored as a content-addressed SHA-256 snapshot (M1). M3 adds optional
cryptographic attestation so any displayed claim ("comment closes on Y per source Z") is
independently verifiable offline.

## Format

- **Statement**: in-toto Statement v1 — subject = `{name: source_url, digest: {sha256}}`,
  `predicateType: https://github.com/JoshDoesIT/ObligationAggregator/attestations/fetch/v1`,
  predicate = `{source_url, adapter, fetched_at, http_status, http_headers}` (the same
  metadata stored on the snapshot row).
- **Envelope**: DSSE v1 (`application/vnd.in-toto+json` payload type) signed with the
  instance's Ed25519 key. PAE encoding per the DSSE spec.
- Stored beside the snapshots: `data/snapshots/attestations/<sha256>.dsse.json`;
  `snapshot.attestation_ref` records the relative path.

## Keys

`oblag keygen` writes an Ed25519 private key (PEM) to `OBLAG_SIGNING_KEY_PATH`
(default `data/keys/signing.pem`) and the public key beside it (`signing.pub`).
Key ID = SHA-256 of the DER-encoded public key.

Signing activates automatically when the key exists; without it, snapshots are
hash-only (attestation_ref stays NULL). `oblag verify-snapshot <sha256>` re-hashes
stored content and verifies the DSSE signature.

## Why a project key, not public-Rekor keyless (challenged from the research docs)

Keyless Sigstore signing requires an OIDC identity on the fetcher and publishes every
monitored URL + timestamp to a public transparency log. That is fine for public
regulations but wrong for private/BYOL obligations, and it couples self-hosters to an
external service. Design: DSSE/in-toto formats (Sigstore-compatible), local key by
default, Rekor submission as a future opt-in flag for public sources only.
BYOL private documents are hashed but NEVER attested to any external log.
