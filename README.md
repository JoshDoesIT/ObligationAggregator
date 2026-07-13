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

| Source | Mechanism | Status |
|---|---|---|
| US Federal Register (`PRORULE`/`RULE`) | JSON API, no key | ✅ |
| NIST CSRC drafts open for comment | JSON feed | ✅ |
| regulations.gov dockets | JSON API (free key) | ✅ |
| EUR-Lex / CELLAR (EU OJ, proposals) | SPARQL | ✅ |
| OEIL watched procedures | RSS/XML | ✅ |
| LegiScan (US state laws) | JSON API (free key) | ✅ |
| PCI SSC / ISO / AICPA change artifacts | scrape (defensive) | ✅ |

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
