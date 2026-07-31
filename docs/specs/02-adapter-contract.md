# Spec 02 — Adapter Contract

Every source is an adapter implementing:

```python
class SourceAdapter(ABC):
    name: str  # e.g. "federal_register"
    jurisdiction: str  # default jurisdiction for items

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        """Fetch raw payloads from the source. May paginate. Uses ctx.client (httpx)."""

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        """Pure function raw → 0..n NormalizedItems. No network. Fixture-testable."""
```

- `RawDocument`: `{url, content: bytes, content_type, fetched_at, meta: dict}`.
- The **runner** (not the adapter) is responsible for: storing each RawDocument in the
  snapshot store, calling `normalize`, feeding items to the reducer, updating
  `adapter_health`, and catching per-item errors (one bad record must not abort the run).
- `NormalizedItem` fields: `source_system, external_key (type, value) — the identity join
  key, join_keys (additional), jurisdiction, title, abstract, url, native_status,
  dates: [NormalizedDate(date_type, value, confidence, label?)], content_fingerprint,
  obligation_slug?, raw_summary?`.
- `content_fingerprint` = SHA-256 over the normalized semantic content (NOT raw bytes), so
  cosmetic feed reordering does not fire `content_changed`.
- `published_at` is **fill-if-None**: the first observed value stands, so a sitemap
  lastmod touch cannot make an old filing look newly published. An adapter whose row
  stands for a living page rather than a fixed document sets `published_at_moves` to opt
  out — but the reducer still only moves the date to one the source states, so an empty
  parse cannot erase a good value.
- Identity: an incoming item matches a stored `pipeline_item` iff any join key
  (type, value) matches. `external_key` must be stable across fetches.
- Adapters MUST be defensive: unknown enum values (e.g. a new NIST draft stage) map to a
  conservative default and emit an `anomaly` event rather than raising.
- **Declaring an expectation.** An adapter that fetches a specific page it knows should
  produce exactly one item sets `meta["expect_item"]` on that `RawDocument`. If it
  normalizes to zero, the runner records it in `RunStats.blind` and on
  `adapter_health.last_error`, while still counting the run a success — the fetch
  worked, the other pages produced good data, and nothing should self-disable.
  Without this, a body redesigning its site is indistinguishable from a body with
  nothing to say: the fetch returns 200, the parse returns nothing, and the row goes on
  serving whatever it last said. That is exactly how the NYDFS watch went blind
  (spec 06). Listing endpoints do NOT set it — an empty page there is ordinary.
- Incremental fetch: `FetchContext.since` (datetime|None) — adapters use source-native
  incremental parameters where available; `FetchContext.window` for bounded backfills.

## Fixture testing

Each adapter ships `tests/fixtures/<adapter>/*.json` recorded from the live API and a test
that runs `normalize` over fixtures asserting exact NormalizedItems. Runner-level tests use
`respx` to mock HTTP.
