# Spec 01 — Data Model

All tables via SQLAlchemy 2.0; SQLite for dev/tests, Postgres for production (Alembic migrations).

## Enums

- `ItemState`: `proposed, comment_open, comment_closed, final_pending_effective, effective,
  withdrawn, stalled, superseded`
- `DateType`: `proposal_date, comment_open, comment_close, projected_final, adopted,
  effective, phased_compliance, entry_into_force, application, transition_deadline`
  (EU: `entry_into_force` ≠ `application` — distinct types to avoid mis-countdowns)
- `Confidence`: `statutory_hard, published_firm, agency_estimate, derived`
- `EventType`: `item_created, state_changed, date_changed, content_changed, item_resolved,
  anomaly`
- `DisplayPolicy`: `full_text, ids_and_titles, ids_only, events_only`
- `CopyrightStatus`: `public_domain, eu_reuse, licensed, copyrighted`

## Tables

### obligation
Framework/regulation registry entry. `slug` unique (e.g. `pci-dss`, `nist-800-53`).
Fields: id, slug, name, issuing_body, jurisdiction, canonical_url, copyright_status,
display_policy, created_at.

### pipeline_item
One tracked lifecycle item (an NPRM, a draft SP, a procedure file, a version release).
Fields: id, source_system, jurisdiction, title, abstract, url, state (ItemState),
native_status (source-native string), content_fingerprint, obligation_id (nullable FK),
resolved_change_id (nullable — set when a proposed item resolves to a final one),
first_seen_at, last_seen_at.

### join_key
(pipeline_item_id, type, value); UNIQUE(type, value). Types: `rin, docket_id, fr_doc_number,
nist_pub_url, celex, oeil_procedure, bill_id, iso_project, pci_doc, legiscan_bill`.

### key_date  (append-only)
pipeline_item_id, date_type, value (date), confidence, source_snapshot_id (nullable FK),
asserted_at, supersedes_id (self-FK, nullable), label (nullable — e.g. phased-deadline name).
Current value of a (item, date_type, label) = the row not superseded by any other row.

### snapshot
sha256 (unique), source_url, adapter, fetched_at, http_status, http_headers (JSON subset:
etag/last-modified/content-type), storage_ref (relative path), attestation_ref (nullable).

### event
pipeline_item_id (nullable for system events), type (EventType), payload (JSON),
snapshot_id (nullable FK), occurred_at. Payload contracts:
- state_changed: {"from": str|null, "to": str}
- date_changed: {"date_type": str, "label": str|null, "from": str|null, "to": str,
  "confidence": str, "superseded_key_date_id": int}
- anomaly: {"kind": str, "detail": str}

### watchlist
id, name, channel (`rss | email | webhook`), target (email addr / webhook URL / rss token),
filters (JSON: source_systems[], jurisdictions[], states[], obligation_slugs[],
event_types[]), active, created_at.

### notification_log
watchlist_id, event_id, delivered_at, status (`sent | failed`), detail. UNIQUE(watchlist_id,
event_id) — at-most-once delivery per event per watchlist.

### adapter_health
adapter (unique), last_run_at, last_success_at, consecutive_failures, last_error,
items_seen_last_run.

### private_document  (BYOL — never in shared outputs)
id, obligation_id FK, version_label, sha256, storage_ref, license_attested_at, uploaded_at.
UNIQUE(obligation_id, version_label).
