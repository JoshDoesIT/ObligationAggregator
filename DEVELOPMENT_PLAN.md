# ObligationAggregator — Development Plan

## Context

The repo (`JoshDoesIT/ObligationAggregator`, branch `claude/app-development-plan-adagke`) is **empty** — this is a greenfield build. Two research documents describe an open-source regulatory change-intelligence platform for GRC engineers: (1) a strategic build plan (obligation catalog, change detection, OSCAL, Sigstore provenance, open-core monetization) and (2) a horizon-scanning architecture (lifecycle state machine over formally proposed regulatory changes, versioned date assertions, multi-source adapters).

The goal of this plan: turn those documents into a buildable, honestly-scoped development plan. Per user decisions: **MVP = Federal Register + NIST CSRC adapters; Python monolith (FastAPI + Postgres + server-rendered UI); hashed snapshots now / Sigstore later; no AI features in MVP.**

I verified the MVP-critical sources live (2026-07-13):
- Federal Register API: works, no key, returns `comments_close_on` / RINs / docket IDs structured. ✅
- NIST CSRC drafts JSON feed: works — and its live data confirms stage is concatenated into `title` AND shows a stage (`/iwd`) missing from the docs' IPD/2PD/FPD enum. Parser must be defensive with an open enum. ✅
- regulations.gov: 403 without key (as documented). CELLAR SPARQL: endpoint up, deeper claims unverified.

## Feasibility challenges (what the docs get wrong or oversell)

These shape the plan; each is reflected in scoping below.

1. **Combined scope is a 12–18 month program, not an MVP.** The docs together describe 10+ source adapters, a lifecycle state machine, OSCAL crosswalks with set-theory relations, Sigstore/in-toto provenance, hybrid semantic search, AI summarization with local inference, multi-tenant SaaS, and a curated-content business. The "~4–6 engineer-weeks" (doc 1 Stage 1) and "2–3 months Phase 0" (doc 2) estimates cover only the adapter work, not the platform around it. This plan sequences ruthlessly and treats each doc estimate as ~2× optimistic.
2. **The two docs' MVPs conflict.** Doc 2's Phase 0 includes EUR-Lex/CELLAR; doc 1 correctly defers EU to Stage 3. EU is deferred here: SPARQL + CDM ontology + an RSS feed the doc itself flags as unverified is a milestone of its own, started with a time-boxed spike.
3. **IAPP tracker cannot be ingested.** Doc 1 lists it as a Stage 2 data source, but it's copyrighted, human-curated HTML with no API — scraping and re-serving it contradicts doc 2's own copyright posture. Demote to a manual QA cross-check, never an ingestion source.
4. **Sigstore-per-fetch has unexamined costs.** Keyless signing needs an OIDC identity on the fetcher; the public Rekor log rate-limits and permanently publishes what/when you monitor (fine for public regs, wrong for the planned "private obligations" feature). MVP: content-addressed SHA-256 snapshots + full fetch metadata, schema shaped so DSSE/in-toto attestation bolts on later without migration (per user decision).
5. **"OSCAL-native" is overstated for an MVP.** The OSCAL mapping model's set-theory crosswalk is effectively a research project, and most tracked obligations (PCI, ISO, state laws) have no OSCAL catalogs at all. MVP: OSCAL-*compatible* — stable identifiers and a JSON export shaped for OSCAL interop; the crosswalk feature is deferred.
6. **Unified Agenda / reginfo.gov, OEIL bulk, ESA, PCI, ISO, AICPA, EDPB are all scrape targets.** Both docs concede this. None enter the MVP; each later adapter must budget for breakage monitoring. PCI RFC content is NDA-gated — only pipeline-stage metadata is ever observable.
7. **Free-text date parsing is a trap.** The FR `dates` field (phased compliance dates) is prose. MVP uses only structured fields (`comments_close_on`, `effective_on`, feed dates); prose parsing is deferred and lands behind human review, not regex heroics.
8. **AI features are correctly risky by the docs' own evidence** (17–33% hallucination in purpose-built legal RAG). Deferred entirely per user decision; MVP is deterministic. This also removes pgvector/embeddings from the MVP schema.
9. **Federal Register `count` caps at 10,000** (observed live), so bulk backfill must window by `publication_date` ranges rather than paging one giant query.
10. **Doc 1's own best idea is load-bearing and must be in from day one:** versioned, sourced `KeyDate` assertions (dates as append-only events, never overwritten) and the state machine. Retrofitting these later genuinely is expensive. They are in Milestone 1.

## Architecture (MVP)

Single Python package/repo, Apache-2.0.

```
Scheduler (APScheduler in-process; cron-compatible)
   └─ Source adapters (federal_register, nist_csrc) — common contract:
        fetch() → raw snapshot (content-addressed, SHA-256, stored + fetch metadata)
        normalize() → NormalizedItem (canonical schema)
   └─ Reducer: compare NormalizedItem vs stored PipelineItem
        → emits Events: item_created | state_changed | date_changed | content_changed
   └─ Linker: RIN-based proposed→final matching (FR PRORULE ↔ RULE)
Postgres (SQLAlchemy + Alembic)
FastAPI app
   ├─ REST API (/api/v1: items, events, key-dates, watchlists) + OpenAPI
   ├─ Server-rendered UI (Jinja2 + htmx): item list/filter, item detail
   │  (lifecycle + date history), "comment windows open now", upcoming
   │  effective dates, provenance drawer (snapshot hash + fetch metadata)
   └─ Outbound: RSS feed, email digest (SMTP), generic webhook
CLI (typer): fetch-once, backfill, replay, serve
Docker Compose for self-host (app + Postgres)
```

### Core data model (tables)

- **obligation** — id, name, issuing_body, jurisdiction, canonical_url, `copyright_status`, `display_policy` (`full_text | ids_and_titles | ids_only | events_only` — drives what UI may display, in schema from day one)
- **private_document** — workspace-local BYOL store: obligation_id, version_label, sha256, storage_ref, license_attested_at; excluded from all shared outputs (see "Copyrighted obligations" section; table created in M0, features in M5)
- **pipeline_item** — id, source_system, jurisdiction, title, abstract, `state` (enum: `proposed, comment_open, comment_closed, final_pending_effective, effective, withdrawn, stalled, superseded`), obligation_id (nullable FK)
- **join_key** — pipeline_item_id, type (`rin, docket_id, fr_doc_number, nist_pub_url, celex, oeil_procedure, bill_id, …`), value; unique(type, value). Multiple keys per item → cross-source correlation.
- **key_date** — pipeline_item_id, date_type (`comment_open, comment_close, projected_final, adopted, effective, phased_compliance, …`), value, `confidence` (`statutory_hard, published_firm, agency_estimate, derived`), source_snapshot_id, asserted_at, `supersedes_id` (self-FK). **Append-only** — a moved date is a new row + a `date_changed` event.
- **snapshot** — sha256 (PK-ish), source_url, fetched_at, http_metadata (etag/last-modified/status), storage_ref, `attestation_ref` (nullable — reserved for Sigstore later)
- **event** — pipeline_item_id, type (`item_created, state_changed, date_changed, content_changed, item_resolved`), payload jsonb, snapshot_id, occurred_at
- **watchlist / subscription** — filter (jurisdiction/source/state/obligation), channel (rss token, email, webhook URL)

State-machine transition table is data-driven config, with the mapping from each source's native status → canonical state exactly as tabulated in doc 1 (FR PRORULE + future `comments_close_on` → `comment_open`, etc.). Illegal transitions log + surface as anomalies rather than crash.

## Milestones

### Milestone 0 — Skeleton (≈1 week)
Repo scaffolding: `pyproject.toml` (Python 3.12, FastAPI, SQLAlchemy, Alembic, typer, httpx, APScheduler, Jinja2, pytest), `LICENSE` (Apache-2.0), `README`, `docker-compose.yml`, GitHub Actions CI (lint ruff, typecheck mypy, pytest), Alembic baseline with the schema above, adapter base contract + fixture-driven test harness (recorded API responses as test fixtures so tests never hit the network).

### Milestone 1 — Federal Register adapter + state machine core (≈3–4 weeks)
- FR adapter: poll `PRORULE` + `RULE` documents (windowed `publication_date` queries — see challenge #9), structured fields only; snapshot every fetch.
- Reducer + events; append-only KeyDates with confidence levels; RIN linker (proposed→final→effective).
- CLI: `fetch-once`, `backfill --from --to`, `serve`.
- Minimal UI: filterable item list + item detail with lifecycle timeline and date history.
- **Acceptance benchmark (from doc 1, kept):** backfill 2022–2026 CISA data and reconstruct the CIRCIA lifecycle (RIN 1670-AA04) — NPRM detected, comment-period extension emitted as `date_changed`, projected-final slip captured, no false `effective`. This is the definition of done for the milestone.

### Milestone 2 — NIST CSRC adapter + notifications (≈2 weeks)
- CSRC JSON feed adapter; defensive stage parser from URL suffix (`/iwd`, `/ipd`, `/2pd`, `/fpd`, `/final`, revisions) with **open enum** + unknown-stage anomaly reporting (per live-feed finding); comments-due date parsed from `content` string.
- Outbound: RSS feed, daily email digest, webhook on watchlist match. Watchlists CRUD in UI.
- Fetch-failure monitoring: adapter health table + surfacing in UI (doc 2's "scrapers break" mitigation, applied to feeds too).

### Milestone 3 — regulations.gov enrichment + provenance upgrade (≈2–3 weeks)
- regulations.gov v4 adapter (free API key, `X-Api-Key`): docket enrichment (RIN from `/dockets/{id}` — v4 quirk, comment counts, `docketType`), `lastModifiedDate` incremental polling, 250/page + 5,000-record pagination handling.
- Provenance upgrade: DSSE/in-toto attestation of snapshots (cosign, project key first; keyless/Rekor optional flag — public-log privacy tradeoff documented). Schema already has `attestation_ref`, so no migration.

### Milestone 4 — EU spike, then decide (time-boxed 1 week spike + ≈4–6 weeks if green-lit)
- Spike: validate CELLAR CDM queries for proposals/procedures, confirm the notification/RSS feed's actual URL and behavior, assess OEIL RSS/XML export stability. Output: go/no-go doc per source.
- If green: CELLAR adapter (OJ publications, entry-into-force/application dates as distinct `date_type`s — EU "entry into force" ≠ "application", per doc 1's caveat) + OEIL watched-procedure adapter. Model AI Act phased dates as the reference multi-stage case.

### Milestone 5 — Copyrighted-obligation value layer (≈3–4 weeks)
Public change-artifact adapters (PCI SSC Summary-of-Changes/bulletins, ISO catalog stage + amendment metadata, AICPA exposure drafts) with scraper breakage monitoring; BYOL private-document store + local version diffing; identifier-level structure diffs gated by `display_policy`. Details in "Copyrighted obligations" section below.

### Deferred (explicitly out of MVP, tracked in README roadmap)
State laws (LegiScan), Unified Agenda/OIRA scraping, ESA/EDPB/Have Your Say, OSCAL crosswalk + Trestle interop, AI summarization/mapping (assistive-draft-only when it lands), pgvector/semantic search, multi-tenancy/SSO/workspaces, hosted SaaS, `/ee` split. IAPP tracker: manual QA cross-check only, never ingested (challenge #3).

## Copyrighted obligations: how to deliver real value anyway

"Metadata + links" undersells what's legally available. Copyright protects the *expression* (the standard's text), not *facts about it* — versions, dates, deadlines, and control identifiers are facts. Four layers, from always-on to opt-in:

1. **Change-event layer (always on, all users).** Version releases, errata, transition/retirement deadlines, future-dated requirements becoming mandatory — e.g., "PCI DSS v4.0.1 published; future-dated requirements mandatory 2025-03-31," "ISO 27001:2022 transition deadline." These are exactly the KeyDate/event primitives the core already has; copyrighted obligations get the full state machine and countdown treatment, just without body text.
2. **Public change-artifact adapters.** Standards bodies freely publish documents *about* their changes: PCI SSC "Summary of Changes" PDFs and bulletin pages, ISO catalog pages (stage codes, amendment/corrigendum metadata), AICPA exposure-draft announcements. Ingest these public artifacts as first-class sources — they turn "a new version exists" into "requirements 8.3.x changed, 6.4.3 is new" without touching the standard itself. (These are scrapers, so they slot into the post-MVP adapter track with breakage monitoring.)
3. **Structure-level diffs on identifiers.** Control/requirement IDs and numbering are facts: render "added 4.2.1.1, removed A.11.2.5, renumbered 6.4.x" diffs and ID-level crosswalks (PCI 8.3.6 ↔ 800-53 IA-5) using OSCAL identifier-only mappings — no requirement text, short titles configurable per `copyright_status` (conservative default: IDs only for ISO, which is litigious).
4. **BYOL (bring-your-own-license) private store — the self-host killer feature.** Users who legitimately own PCI DSS/ISO copies drop their licensed PDFs into a workspace-local private store; the tool then does full-text extraction, version diffing, and control-level change mapping *locally*. Nothing is redistributed — the open-source tool ships the capability, the user supplies the licensed content. Private documents are hashed for provenance but never enter shared snapshots, exports, RSS, or webhooks. This converts the copyright constraint into a differentiator and fits the local/privacy-preserving posture the docs already want for AI.

Plan impact: `copyright_status` already gates the UI (Milestone 0 schema). Add now: a `private_document` table (workspace-local, content hash, storage ref, license-attestation flag) and a `display_policy` per obligation (`full_text | ids_and_titles | ids_only | events_only`) so layers 3–4 need no migration. Layer 1 works from Milestone 1 primitives; layers 2–4 land as **Milestone 5 (post-EU-spike, ≈3–4 weeks: PCI Summary-of-Changes + ISO catalog adapters, BYOL store + local diff)**. Original human/community-written change annotations (new expression, not copies) remain the curated-content play from doc 2, unchanged.

## Critical files to create (Milestone 0–1)

```
pyproject.toml, LICENSE, README.md, docker-compose.yml
.github/workflows/ci.yml
src/oblag/db/models.py            # tables above
src/oblag/db/migrations/          # alembic
src/oblag/adapters/base.py        # fetch/normalize contract, snapshot store
src/oblag/adapters/federal_register.py
src/oblag/adapters/nist_csrc.py   # M2
src/oblag/core/reducer.py         # state machine + event emission
src/oblag/core/linker.py          # RIN join
src/oblag/core/statemap.py        # source-status → canonical-state config
src/oblag/api/                    # FastAPI routers
src/oblag/web/                    # Jinja2 templates + htmx
src/oblag/cli.py
tests/fixtures/                   # recorded API payloads (incl. CIRCIA docs)
tests/
```

## Verification

- **Unit/integration:** pytest against recorded fixtures (no live network in CI). State-machine reducer gets exhaustive transition tests including illegal-transition anomalies and `date_changed` supersession chains.
- **CIRCIA end-to-end benchmark (M1 gate):** `oblag backfill --agency cisa --from 2024-01-01`, then assert the event stream contains the known lifecycle: NPRM 2024-04-04 → comment close 2024-06-03 → extension to 2024-07-03 (`date_changed`) → projected-final slip. Scripted as a repeatable test.
- **Live smoke:** `oblag fetch-once federal_register && oblag fetch-once nist_csrc` against real APIs; verify new items appear in UI, RSS validates, webhook fires on a watchlist match; provenance drawer shows snapshot hash + fetch metadata for every displayed date.
- **Self-host check:** `docker compose up` from a clean clone → working UI + scheduler within one command.

## Not doing (and why) — summary for the README

No standards body text is ever stored/redistributed for copyrighted sources (`copyright_status` gates UI). No AI in MVP (deterministic signals only). No EU/state/scrape sources until the state-machine core proves itself on the CIRCIA benchmark. Projected dates are always displayed with confidence labels — an agency estimate is never rendered as firm.
