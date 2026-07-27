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
- **pci_docs** (weekly): the PCI standards themselves. `pci_ssc` reads the Perspectives
  blog for RFC announcements, so a standard only appeared while it was under
  consultation — six of the twelve PCI obligations had never shown anything at all
  (3DS, MPoC, P2PE, PIN, PTS POI, TSP).

  The `/document_library/` page is a nav menu: its rows load client-side and the
  documents API behind it answers 403. But the same site publishes
  `/rssfeed/?type=document`, 436 entries covering every document the SSC has released,
  each with a publication date, a category, a document type and a stable `document=`
  slug. That slug is the identity a standard keeps across revisions, so it is the
  external key — a new edition updates the row and its date rather than stacking a
  second one beside it.

  Zero-noise by construction, matching the rest of this layer: the feed is 84 guidance
  documents, 91 programme/certification papers, FAQs, SAQs, reporting templates and case
  studies. Only documents typed `Standard` AND named in `DOCUMENT_OBLIGATIONS` become
  items, so a new document category cannot quietly start filing rows against a standard.
  One wrinkle: the MPoC standard and its summary of changes share a single `document=`
  slug, so the title has to disambiguate them.

- **iso_catalog** (weekly): editions, amendments and publication dates for *watched*
  standards (obligations with an iso.org canonical_url), read from **IEC — the
  co-publisher** — because iso.org itself cannot be read:
  - `www.iso.org` is behind a Cloudflare managed challenge. Our User-Agent and a full
    Chrome one both get 403, and `/robots.txt` itself returns the challenge page.
  - `standards.iso.org` IS reachable, and publishes `User-agent: * / Disallow: /`.
    That is a crawl policy stated the standard way, so it is a no, not a "not yet".
  - `obp.iso.org` is an Angular shell whose `/api/*` returns 401 without a login.
  - ISO/IEC 27001, 27002, 27017, 27018, 27701 and 42001 are ISO/IEC JTC 1/SC 27 *joint*
    publications, so **IEC is an equally authoritative source of record**.
    `webstore.iec.ch/robots.txt` is empty (no restriction) and its search is backed by
    a public JSON API (`POST https://webstore-search-api.iec.ch/api/search`), so this
    needs no browser and runs on serverless.

  Number search is a substring search, so only references matching
  `^(ISO/IEC|ISO) <number>(:|/|$)` are kept, and `-HBK`/`-GUIDE` (handbooks and guides
  that quote a standard without being it) are dropped. `validOnly` keeps superseded
  editions from reading as new filings. Base editions keep the legacy
  `("iso_project", canonical_url)` key so the switch of source refreshes the existing
  rows in place; amendments and corrigenda are separate publications and get their own
  `("iec_pub", <id>)` key. Status → state: `published` → effective, `withdrawn` →
  withdrawn; the ISO harmonized stage codes (60.60 → effective, 95.x → withdrawn, …)
  stay mapped for rows ingested while iso.org was still readable.

  **Coverage, measured live, not assumed:** 27002, 42001, 27701, 27017 and 27018 return
  current editions with publication dates; 27001 returns `ISO/IEC 27001:2022/AMD1:2024`
  (an amendment to the most-watched standard in the catalog that no other source
  surfaced). Two gaps stay operator-curated: **ISO 22301** is ISO-only so IEC has no
  listing, and IEC's store carries 27001's 2013 and 2005 editions but not the 2022 one.
  Both emit **nothing** rather than a placeholder item, so the curated rows survive
  intact instead of being overwritten by a stub.

  One trap worth naming: an ISO version is the year in its *reference*, not the year it
  was printed. `ISO/IEC 27001:2022/AMD1:2024` amends the 2022 edition, and reading the
  version off `publication_date` would have proposed a bogus 2024 bump
  (`versionsuggest._published_version`).
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
  - **NERC** — **retired in v0.21.0.** The adapter worked (the relocated
    standards-under-development page is static and the projects parsed cleanly), so this
    is a scope decision rather than a technical one: CIP is cybersecurity, but NERC's
    standards-development process is a different world from the rest of the catalog and
    the ten projects it produced read as noise beside EU acts and ISO editions. The
    obligation is listed in `catalog.RETIRED_OBLIGATIONS`, which removes it, its items,
    and every org-scope and watchlist reference on the next boot.
  - **CIS** — blog RSS with a strict "CIS Controls vX" release filter (zero-noise by
    design; community posts and vulnerability advisories never match).
- **aiuc** (weekly): the AIUC-1 changelog. No feed exists (`/rss.xml`, `/feed`,
  `/atom.xml` all 404, no `<link rel=alternate>`), but the page is server-rendered
  Next.js, so the release history is in the HTML and no browser tier is needed.

  AIUC-1 is the only obligation here that revises on a **fixed quarterly cadence** and
  **announces its next release date in advance**, so the adapter reads two things from
  one page. Releases in the "Standard history" table plus the current one become
  `release` items dated by their release date. The announced next release becomes a
  `scheduled` item carrying a `projected_final` date, keyed as
  `("aiuc_release", <its own date>)` — the key its released form will carry, so the row
  flips in place rather than a second row appearing beside a stale one. Only the
  "Standard history" section and the two headline sentences may create releases:
  scanning the whole page for dates would sweep up dates quoted inside change notes.

  AIUC-1 also has **no version numbers** — a release IS its date — which is why
  `versions.version_key` learned an ISO-date scheme. It is trusted only when the date
  is the whole value, so "AIUC-1 2026-10-15 (scheduled)" is read as a title and not as
  a published version, and a date is never compared against a dotted baseline.
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
  unchanged — these items are metadata-only by construction. v0.14.0: the scan
  ignores the sitemap since-window (releases/advisories are final-track history, not
  appearance signals; the window permanently hid the v11.8.0 release advisory, lastmod
  8 May, from every scheduled run), floors at CSF major ≥ 9, and records the sitemap
  lastmod as each item's `published_at` so "newest" rankings follow the source's
  chronology instead of ingestion batches.

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
