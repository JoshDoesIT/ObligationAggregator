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
- **AICPA: intentionally NOT built.** The exposure-drafts page is client-side rendered
  with no static payload (probed 2026-07-14); a headless-browser scraper fails the
  maintenance-budget rule. SOC 2 TSC changes are rare — track via curated
  `assert-date` / manual items until AICPA ships a parseable page.
- **ESA (EBA/ESMA) and EDPB consultations: same verdict** (probed 2026-07-14: EBA
  listing is JS-rendered with no static links/dates; EDPB filtered listing 404s).
  DORA RTS/ITS and delegated/implementing-act feedback periods are largely covered by
  the Have Your Say adapter (brpapi JSON); the remainder is the curated
  `assert-date` workflow. Revisit if either body ships a feed/API — headless-browser
  scrapers are below the maintenance-budget line.

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
