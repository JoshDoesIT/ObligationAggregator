# Spec 00 — System Overview

ObligationAggregator (`oblag`) is an open-source regulatory & framework change-intelligence
platform for GRC engineers. It tracks **formally proposed and adopted changes** to laws,
regulations, and security/privacy standards as a **lifecycle state machine** (not a document
differ), with **versioned, sourced date assertions** and **content-addressed provenance** for
every claim it makes.

## Core loop

```
scheduler → adapter.fetch_raw() → snapshot store (SHA-256, content-addressed)
          → adapter.normalize() → NormalizedItem
          → reducer (compare vs stored PipelineItem)
          → events: item_created | state_changed | date_changed | content_changed | item_resolved
          → linker (cross-source join keys, e.g. RIN proposed→final)
          → notifications (RSS / email digest / webhook) on watchlist match
```

## Invariants (enforced by tests)

1. **Dates are append-only assertions.** A changed date NEVER overwrites; it inserts a new
   `key_date` row with `supersedes_id` set and emits a `date_changed` event.
2. **Every displayed fact traces to a snapshot.** `key_date.source_snapshot_id` and
   `event.snapshot_id` are populated whenever the fact came from a fetch.
3. **Copyright is enforced by data, not discipline.** `obligation.display_policy` gates what
   the UI/API may render; copyrighted body text is never stored in shared tables.
   BYOL private documents live only in `private_document` + local storage and are excluded
   from all shared outputs (RSS, webhooks, API exports).
4. **Confidence is explicit.** Every date carries `statutory_hard | published_firm |
   agency_estimate | derived`; an agency estimate is never rendered as firm.
5. **Illegal state transitions do not crash** — they are recorded as anomalies
   (`adapter_health` + event payload) and surfaced.
6. **No network in unit tests.** Adapters are tested against recorded fixtures.

## Scope boundary ("no weak signals")

In scope: FR `PRORULE`/`RULE`, rulemaking dockets, NIST drafts open for comment,
adopted-not-yet-effective laws, EU procedure files, standards with formal comment stages.
Out of scope by default: speeches, ANPRMs/prerule stage (config-includable), committee
agendas, informal papers.
