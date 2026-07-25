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
- **AICPA: resolved via sitemap (M9).** Root cause established by intercepting the
  SPA's GraphQL traffic: its `getStaticLandingPage(slug:"exposure-drafts")` query
  **500s server-side** ("Cannot read properties of null") — the landing page is broken
  upstream, and the GraphQL API needs a browser session. The sitemap, however, lists
  every exposure-draft page; the adapter ingests slug-anchored `…exposure-draft…`
  URLs (44 live). Comment deadlines arrive via curated `assert-date`, which upgrades
  items into the comment-window lifecycle. Caveat: AICPA's sitemap is malformed XML
  (raw `&` in a slug) — `sitemap_base` degrades to tolerant regex extraction.
- **HITRUST: resolved via sitemap (M9).** No feed and WP REST disabled (probed), but
  the sitemap carries formal signals in slugs: CSF version releases
  (`…csf-v11.3.0-launch`, `…release-of-version-11.4.0…`) and version-tied HAA
  advisories. Marketing/case-study slugs never match. `events_only` display posture
  unchanged — these items are metadata-only by construction.

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

## 4. BYOL private store + local diff — **REMOVED in v0.9.0**

Shipped in M5 and withdrawn after evaluation against real documents. `oblag byol
add/diff` let a self-hoster store a licensed copy and get an identifier-level
added/removed/kept report, gated by `display_policy`.

**Why it was removed.** Validated on NIST SP 800-171 r2 → r3 (public domain, so the
comparison could be published): the diff reported **136 added / 137 removed / 18 kept**
for a revision that in fact carried **128 controls forward**. NIST renumbered
`3.1.1` → `03.01.01`, and identifiers were compared as literal strings. The same run
surfaced 19 table-of-contents lines parsed as requirements and 33 "Withdrawn"
placeholders counted as additions.

Normalising identifiers would have fixed that one case, but not the general problem:
each publisher family (PCI tables, ISO Annex A, AICPA TSC, HITRUST hierarchies) needs
its own extractor plus golden fixtures, PDF extraction is lossy in family-specific
ways, and layouts drift silently between revisions. The failure mode is not an empty
result — it is a confidently formatted wrong one, in a product whose entire premise is
accuracy. Dependability would have had to be re-earned per publisher, per revision,
indefinitely.

Layers 1–3 (change events, public change-artifact adapters, identifier facts) are
unaffected and remain the copyrighted-obligation story. Version tracking never
depended on uploads: it comes from the adapters and the auto-apply pass.
