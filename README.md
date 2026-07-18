# ObligationAggregator

**Open-source regulatory & framework change-intelligence for GRC engineers.**

ObligationAggregator (`oblag`) tracks formally proposed and adopted changes to the laws,
regulations, and security/privacy standards you actually run programs against — PCI DSS,
SOC 2, ISO 27001, NIST CSF/800-53, HIPAA, GDPR, DORA, NIS2, the EU AI Act, US state privacy
laws — and turns them into a **lifecycle state machine** with **versioned date assertions**
and **verifiable provenance**, not another newsletter.

## Why

Commercial regulatory-intelligence platforms (CUBE, Corlytics, …) are financial-services-first,
six-figure-priced, and closed. GRC engineers track changes through newsletters, law-firm
alerts, and page-watching. `oblag` replaces that with:

- **A pipeline state machine, not a document differ.** Each tracked item (an NPRM, a draft
  NIST SP, an EU procedure file, an adopted-not-yet-effective law) moves through
  `proposed → comment_open → comment_closed → final_pending_effective → effective`
  (with `withdrawn` / `stalled` / `superseded`), joined across sources by stable identifiers
  (RIN, docket ID, CELEX, procedure reference).
- **Dates as versioned, sourced assertions.** A slipping projected date never overwrites the
  old one — it supersedes it and emits a `date_changed` event you can alert on. Every date
  carries an explicit confidence level (`statutory_hard` … `agency_estimate`); an agency
  estimate is never rendered as firm.
- **Provenance for every claim.** Every fetch is stored as a content-addressed SHA-256
  snapshot with full fetch metadata (DSSE/in-toto attestations optional), so "comment closes
  on date Y per source Z" is independently verifiable.
- **Copyright enforced by data, not discipline.** Copyrighted standards (ISO, PCI DSS) are
  tracked as change events, dates, and requirement identifiers — never body text. Self-hosters
  who own licensed copies can use the private BYOL store for local full-text diffing; private
  documents never enter shared outputs.

## Quick start

```bash
# self-host with Postgres
docker compose up

# or run locally (SQLite)
uv sync
uv run oblag init-db
uv run oblag fetch-once federal_register
uv run oblag serve         # UI + API on http://localhost:8000
```

## Sources

| Source | Mechanism | Enable via |
|---|---|---|
| US Federal Register (`PRORULE`/`RULE`) | JSON API, no key | on by default |
| NIST CSRC drafts open for comment | JSON feed | on by default |
| regulations.gov dockets (enrichment) | JSON API | `OBLAG_REGSGOV_API_KEY` (free) |
| EUR-Lex / CELLAR (EU acts + proposals) | SPARQL | on by default |
| OEIL watched procedures | HTML (defensive) | `OBLAG_OEIL_PROCEDURES="2021/0106(COD),…"` |
| EU Have Your Say feedback periods | JSON API (brpapi) | on by default (`OBLAG_HYS_TOPICS`, default `DIGITAL`) |
| LegiScan (US state laws, passed/enrolled only) | JSON API | `OBLAG_LEGISCAN_API_KEY` + `OBLAG_LEGISCAN_STATES="CA,RI,…"` |
| PCI SSC RFC announcements | blog RSS, formal signals only | on by default (weekly) |
| ISO catalog stage codes | HTML (defensive) | on by default (weekly, watched standards) |
| EDPB consultations & adopted guidance | news RSS, formal signals only | on by default |
| ESMA consultations (incl. DORA RTS/ITS) | site RSS, "consults" filter | on by default |
| EBA consultations | **headless-browser rendered** | `uv sync --extra browser` (self-disables without it) |
| CPPA (California) rulemaking packages | static HTML | on by default (weekly) |
| NERC standards under development | static HTML | on by default (weekly) |
| CIS Controls version releases | blog RSS, strict release filter | on by default (weekly) |
| AICPA exposure drafts | — | still curated: the SPA never hydrates content even in a real browser (spec 06); use `assert-date` |
| Unified Agenda / OIRA projected dates | — | no API; curated `oblag assert-date … --confidence agency_estimate --note "<citation>"` |

## Beyond the feed

- **Curated date assertions** — `oblag assert-date` records dates from sources without
  adapters (Unified Agenda, IAPP cross-checks) with confidence + citation; same
  append-only supersession and `date_changed` events as fetched dates.
- **OSCAL export** — `oblag export-oscal` / `GET /api/v1/export/oscal`: valid OSCAL
  1.1.2 catalog with tracked items as back-matter resources (stable UUIDs; state,
  dates+confidence, join keys as namespaced props). Control-level crosswalk mapping is
  deliberately out of scope until it can be human-reviewed.
- **BYOL private analysis** — `oblag byol add pci-dss 4.0.1 ./licensed.pdf
  --attest-license`, then `oblag byol diff pci-dss 4.0 4.0.1` for identifier-level
  change reports, gated by each obligation's `display_policy`.
- **Provenance** — `oblag keygen` enables DSSE/in-toto attestation of every snapshot;
  `oblag verify-snapshot <sha256>` re-verifies content + signature offline.
- **AI assist (off by default)** — `oblag ai-summarize <item>` drafts a summary with
  mandatory snapshot citations and a non-advice disclaimer; supports Anthropic or any
  OpenAI-compatible endpoint (local Ollama/vLLM for privacy). Never auto-published.
- **Event severity** — every event carries a derived severity
  (`new_obligation | substantive | editorial | operational`) in the API.

## Roadmap (not yet built, and why)

- **AICPA / HITRUST adapters** — AICPA's exposure-drafts SPA never hydrates content
  even in headless Chromium (bot/geo-gated content API); HITRUST publishes no feed.
  Both tracked via curated `assert-date` + BYOL (see `docs/specs/06`). Everything else
  from the original "unparseable" list (EBA, ESMA, EDPB, CPPA, NERC, CIS) now has an
  adapter — feed-first, headless-browser tier where genuinely needed.
- **OSCAL control-level crosswalk** (set-theory relations) — deliberately curated-only
  until mappings can carry human review; the OSCAL catalog export ships today.
- **Multi-tenant workspaces / SSO / hosted SaaS** — self-host single-workspace first.
- **Public-Rekor keyless attestation** — opt-in later for public sources only; local
  DSSE/in-toto signing ships today (see `docs/specs/04` for the privacy rationale).

## Development

```bash
uv sync --all-extras
uv run pytest          # unit tests are fixture-driven; no network
uv run ruff check .
uv run mypy
```

Specifications live in [`docs/specs/`](docs/specs/) — code changes that alter behavior must
update the spec and its tests first. The full development plan (including feasibility
critique of the source research) is in [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md).

## What this project deliberately does not do

- Redistribute copyrighted standards text (ISO, PCI DSS) — metadata, identifiers, dates,
  and links only; `display_policy` gates rendering per obligation.
- Present weak signals (speeches, ANPRMs, committee agendas) as pipeline items by default.
- Trust AI: any AI assistance (optional, off by default) is an assistive draft with
  mandatory source citations, never auto-published.

## License

Apache-2.0
