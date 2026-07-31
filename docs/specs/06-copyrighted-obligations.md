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
- **standard_pages** (weekly): standards whose only publication surface is one page.
  CIS Controls, the CSA Cloud Controls Matrix and NYDFS 23 NYCRR 500 have no feed, no
  API and no document library — just a page saying what the current version is. All
  three showed nothing, because every adapter we had was looking for a stream of events.

  Each entry names the page, the obligation, and a pattern that extracts what the page
  states. When the page starts saying something different, that IS the change signal —
  the same idea as iso_catalog and nist_pubs, generalised to bodies that publish nothing
  else.

  Some bodies state currency by **where a stable link points** rather than in prose, so
  an entry can match the URL the fetch resolved to instead of the body (`match_url`).
  NYDFS is the case that forced it. It used to be read as a dated sentence ("On
  November 1, 2023, DFS announced amendments to Cybersecurity Regulation"), then DFS
  rebuilt its site: the old URL became a link hub and the word "amendment" now appears
  on none of its cybersecurity pages. The regulation is served as one consolidated PDF
  under a dated CMS path, so `/cybersecurity/23-NYCRR-Part-500` — a stable alias that
  302s to it — is the surface. Reissuing the text moves the path, and that is the
  signal. The pattern anchors on the filename as well as the date, because DFS files
  everything under `/documents/YYYY/MM/`.

  A `match_url` date comes from the document's own `Last-Modified`, which says the body
  rewrote the file and nothing about when the regulation takes effect. It sets
  `published_at` only; asserting it as `effective` would invent a source statement.

  Watched-page rows set `published_at_moves` (spec 02). They stand for a living page
  rather than a fixed document, so when the body reissues its standard the date has to
  follow the title. Without it, the NYDFS row read "posted 16 July 2026" while its
  `published_at` still said 2023-11-01 — self-contradictory, and sorted into the
  archive.

  Two guards, both from live evidence:
  * A pattern must anchor on words the body uses about its own standard, never a bare
    version number. The CSA page carries `?ver=4.0.13` on a WordPress asset, and a loose
    `v(\d+\.\d+)` matched that instead of the standard.
  * A page saying **"There is a new version of…"** outranks everything else on it and
    yields nothing. CSA leaves superseded artifact pages up with that notice, and
    without the guard we would have published CCM v4.0 (2021) as current while v4.1
    (2026) was out — the exact staleness this adapter exists to catch.

  No match yields nothing rather than a guess — but it no longer yields nothing
  *quietly*. Each page is fetched with `meta["expect_item"]`, and the runner records any
  such page that parsed to zero items on adapter health (see spec 02). That is what was
  missing when DFS rebuilt its site: the fetch was a 200, the run was a success, and the
  row just stopped updating while still serving what it last said.

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

- **FedRAMP: resolved via sitemap.** No feed exists — `/news`, `/rss.xml`, `/feed.json`
  and `/documents` all 404, and the Rev 5 documents page states no version anywhere in
  its markup. The sitemap (1116 URLs) does carry the signal: every announcement slug
  begins with its own date (`/2026-06-25-propelling-change-…-consolidated-rules…/`, and
  older ones under `/archive/`).

  The filter is where the work is. Most FedRAMP announcements are programme news — a new
  leader, an RFQ, a shutdown notice, "authorizations hit 300", the annual survey recap —
  and none of it changes what an agency or a CSP must do, so per spec 00 none of it is an
  item. A slug has to name a thing that changes the requirement: a baseline, a revision,
  the rules, a policy, a directive response. On the live sitemap that takes 52 dated
  announcements down to 17. Two details cost a re-run to find: upstream casing is
  inconsistent (`…-updated-3PAO-obligations-and-performance-standards-document`), so the
  slug pattern is case-insensitive and the slug is lowercased for identity; and lastmod
  drifts (two 2025 announcements carry the crawl date), so `published_at` comes from the
  slug's own date and never from lastmod.

- **Obligations that ARE a CFR part: the eCFR versioner API.** The GLBA Safeguards Rule
  is 16 CFR 314, the SEC cybersecurity disclosure requirement is Item 106 of Regulation
  S-K, and the SOX internal-control obligations are Exchange Act rules 13a-15/15d-15 plus
  Reg S-K Item 308. All three showed nothing, because the only adapter that could see
  them is the Federal Register and a rulemaking scrolls out of its window in a couple of
  years while the rule stays in force. Item 106 was adopted in August 2023 and had
  already fallen out of view.

  `/api/versioner/v1/versions/title-17.json?part=229` answers the right question
  directly: when was each section last amended. The most recent amendment across a
  watched target IS the state of that obligation, and a new one updates the row.

  A watched target names the part and, where a part is enormous and only one section is
  the obligation, the exact sections. Regulation S-K is 200+ sections about executive pay
  and mine safety; watching the whole part would report every unrelated SEC amendment as
  a change to the cybersecurity rule.

- **UK GDPR: legislation.gov.uk changes feeds.** UK GDPR had no source of any kind. The
  text lives at legislation.gov.uk as retained Regulation 2016/679 alongside the Data
  Protection Act 2018, and neither publishes a newsroom, but every piece of legislation
  has a changes feed whose entries carry a structured `ukm:Effect`: the amending
  instrument, the provisions touched, what was done to them, and commencement.

  One row per amending instrument, not per provision. The Data (Use and Access) Act 2025
  consequential regulations amend 48 articles of UK GDPR; 48 near-identical rows would
  bury the fact a reader needs. Grouping also stabilises identity, because the same
  instrument reappears as more of its provisions commence.

  The feed distinguishes "not commenced yet" from "no date known", and so does the
  adapter: those DUAA effects are marked `Applied="false" Prospective="true"` with no
  date, which means commencement on a day to be appointed. That is recorded as pending
  with no compliance date rather than guessed at.

- **NIST AI RMF: a watched page.** AI 100-1 has no CSRC series index (`/publications/ai`
  is a 404), so `nist_pubs` cannot see it, and the framework landing page is the only
  surface that states the version. Added to `standard_pages`.

  That work also hardened version extraction generally. Two fetches of the same CIS URL
  minutes apart came back as different CDN variants, and one mentioned "CIS Controls
  v7.1" before v8.1 — first-match would have published a superseded edition as the
  current standard. Version pages now take the **highest** version they state, since a
  page about v8.1 mentions older versions all the time and never a newer one.

## Statutes with no machine-readable source at all

Four obligations have no feed, no API, no document library and no version page, because
the publisher's only artifact is the statute text: **PIPEDA**, **the LGPD**, **US state
comprehensive privacy laws**, and the **EU AI Act**'s phased deadlines. Justice Canada
and Planalto publish consolidated law, not change streams; fifty state legislatures need
a LegiScan key and even then produce bills rather than compliance dates.

These get curated milestone timelines (`oblag/milestones.py`) instead of an adapter.
They are seeded at boot through the ordinary reducer, so they get items, events,
deadlines, ICS export and watchlists like any fetched signal, and append-only date
assertions keep re-seeding idempotent. Every entry carries a citation URL and states
what is *not* known: PIPEDA's data-mobility framework (Bill C-15) has Royal Assent
recorded as `adopted` and no in-force date, because it commences on a day fixed by order
of the Governor in Council and that day does not exist yet. Asserting a plausible one
would be exactly the confidently-wrong output this project refuses to produce.

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
