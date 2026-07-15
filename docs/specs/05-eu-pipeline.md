# Spec 05 — EU Pipeline (CELLAR + OEIL)

## Spike results (live, 2026-07-14) — GO

- **CELLAR SPARQL** (`https://publications.europa.eu/webapi/rdf/sparql`): works. Acts
  (REG/DIR/DEC + delegated/implementing) queryable by `cdm:work_date_document` window with
  CELEX, English title, and date properties. Proposals (PROP_REG/PROP_DIR, CELEX sector 5
  `…PC…`) queryable the same way.
- **Key discovery:** `cdm:resource_legal_date_entry-into-force` is **multi-valued** and
  encodes phased application. AI Act (32024R1689) returns 2024-08-01 (EIF), 2025-02-02,
  2025-08-02, 2026-08-02, 2027-08-02 — the full phased timeline;
  `cdm:resource_legal_date_deadline` carries further compliance deadlines (2027…2031).
- **Corrigenda** appear as CELEX with `R(NN)` suffix (e.g. `32024R3110R(02)`) — surfaced
  as anomaly-note events on ingest ("corrigendum published"), not separate items (v1).
- **OEIL**: no bulk API; EP Open Data v2 procedure lookup 404s. The procedure-file HTML
  (redirects to oeil.europarl.europa.eu) contains a parseable "Stage reached" value and a
  dated key-events list → conservative watched-procedure scraper only.
- CELLAR RSS/notification feed: NOT validated; polling windows suffice at daily cadence.

## Date semantics (spec 01 reminder)

`entry_into_force` ≠ `application`. Mapping from CELLAR:
- earliest entry-into-force value → `entry_into_force`
- later entry-into-force values → `phased_compliance`, ordinal labels (`application-1…`),
  so a Digital-Omnibus-style shift emits `date_changed` on that ordinal
- `resource_legal_date_deadline` values → `transition_deadline`, ordinal labels
- `work_date_document` → `adopted` (acts) / `proposal_date` (proposals)

## States

- CELLAR proposals → `proposed` (COD progress detail comes from OEIL)
- CELLAR acts → `final_pending_effective` until earliest EIF passes, then `effective`
- OEIL stage-reached (data-driven map): "Awaiting …" stages → `proposed`;
  "Procedure completed, awaiting publication" / "Awaiting signature" →
  `final_pending_effective`; "Procedure completed" → `effective`;
  "Procedure lapsed or withdrawn" → `withdrawn`; unknown → anomaly (open enum).

## Watched procedures

OEIL fetches only procedures listed in `OBLAG_OEIL_PROCEDURES` (csv of references like
`2021/0106(COD)`) or passed via CLI. Scraping the whole observatory is out of scope.
