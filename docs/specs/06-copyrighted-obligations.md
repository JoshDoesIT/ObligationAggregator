# Spec 06 — Copyrighted-Obligation Value Layer

Copyright protects the *text* of PCI DSS / ISO / SOC 2 TSC, not facts about them.
Four layers (DEVELOPMENT_PLAN.md), implemented as:

## 1. Change events (M1 primitives — already live)
Version releases, transition deadlines, RFC windows as pipeline items/KeyDates.

## 2. Public change-artifact adapters (this milestone)

- **pci_ssc** (weekly): PCI Perspectives blog RSS. Only titles matching formal-signal
  patterns become pipeline items: "Request for Comments: <standard>" → RFC item,
  `comment_open` at pubDate with a **derived** comment_close (+30d, the RFC minimum —
  explicitly Confidence.derived, never presented as firm). Everything else in the blog
  is ignored (no weak signals). Verified live: "Request for Comments: PCI DSS v4.0.1"
  present in the feed (2026-06-03).
- **iso_catalog** (weekly): iso.org catalog pages for *watched* standards (obligations
  with an iso.org canonical_url). Parses harmonized stage code, edition, publication
  date. Stage → state map (open enum): 40.20 DIS ballot → comment_open; 40.6x/40.9x →
  comment_closed; 50.x → final_pending_effective; 60.x → effective (60.60 published);
  90.x (review) → effective; 95.x → withdrawn. Edition changes → content_changed.
- **Formerly-unparseable sources (resolved in M8, feed-first + browser tier):**
  - **EDPB** — news RSS (`/feed/news_en`), filtered to formal signals (consultation
    launches with parsed deadlines, adopted guidelines) on obligation `gdpr`.
  - **ESMA** — site RSS filtered to "consults" titles; DORA-matched items link to
    obligation `dora`. Dates from embedded `datetime` attributes.
  - **CPPA** — the regulations page is static HTML: Proposed/Completed rulemaking
    packages ingested; "Preliminary Rulemaking Activities" excluded as pre-rule
    weak signals (spec 00).
  - **EBA** — genuinely JS-rendered (Drupal 10, JSON:API disabled): fetched via the
    **headless-browser tier** (`oblag[browser]`, spec 06 addendum below). Rows carry
    EBA/CP references (durable join keys) and consultation windows.
  - **NERC** — the relocated standards-under-development page is static; development
    projects ingested conservatively; ballot/comment dates via curated assertions.
  - **CIS** — blog RSS with a strict "CIS Controls vX" release filter (zero-noise by
    design; community posts and vulnerability advisories never match).
- **AICPA: still curated.** Even rendered in headless Chromium the exposure-drafts
  SPA never hydrates content (probed 2026-07-18: only tracking scripts + empty app
  state after selector waits) — the content API is bot/geo-gated. Track SOC 2 TSC
  via curated `assert-date` until AICPA ships a parseable page.
- **HITRUST: still curated** — no feed, no parseable page, `events_only` posture.

## Headless-browser tier (addendum)

`src/oblag/browserfetch.py`: last-resort rendering for sources with no feed, API, or
static payload. Optional extra (`pip install 'oblag[browser]'`;
`docker build --build-arg WITH_BROWSER=true`). Browser-gated adapters self-disable
cleanly without it. Rendered snapshots are DOM serializations, flagged
`x-oblag-rendered: true` in snapshot headers/provenance. Behind TLS-intercepting
egress proxies, Chromium's TLS 1.3 post-quantum ClientHello is capped to TLS 1.2 on
the client→proxy leg (diagnosed via netlog; the proxy re-originates TLS upstream).

## 3. Identifier-level structure (facts, not expression)

`oblag/structure.py` extracts requirement/control identifiers (PCI `8.3.6`,
ISO Annex `A.5.23`, TSC `CC6.1`) from text — line-anchored to avoid false positives.
IDs are facts; body text is never extracted into shared storage.

## 4. BYOL private store + local diff

- `oblag byol add <obligation> <version> <file> --attest-license` copies the user's
  licensed copy into `data/private/`, hashes it, records `license_attested_at`.
- `oblag byol diff <obligation> <v1> <v2>` extracts identifiers from both versions
  locally and reports added/removed/kept — output gated by the obligation's
  `display_policy`:
  - `events_only` → counts only
  - `ids_only` → identifier lists (ISO default)
  - `ids_and_titles` → identifiers + their heading line (PCI/SOC 2 default)
  - `full_text` → identifiers + heading line (BYOL diffs never dump body text)
- Private documents are not pipeline items, never enter snapshots/RSS/webhooks/API
  exports, and are never attested to any external log (spec 04).
