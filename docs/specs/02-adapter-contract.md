# Spec 02 — Adapter Contract

Every source is an adapter implementing:

```python
class SourceAdapter(ABC):
    name: str                     # e.g. "federal_register"
    jurisdiction: str             # default jurisdiction for items

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
- Identity: an incoming item matches a stored `pipeline_item` iff any join key
  (type, value) matches. `external_key` must be stable across fetches.
- Adapters MUST be defensive: unknown enum values (e.g. a new NIST draft stage) map to a
  conservative default and emit an `anomaly` event rather than raising.
- Incremental fetch: `FetchContext.since` (datetime|None) — adapters use source-native
  incremental parameters where available; `FetchContext.window` for bounded backfills.

## Fixture testing

Each adapter ships `tests/fixtures/<adapter>/*.json` recorded from the live API and a test
that runs `normalize` over fixtures asserting exact NormalizedItems. Runner-level tests use
`respx` to mock HTTP.
