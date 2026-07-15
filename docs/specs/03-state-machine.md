# Spec 03 — Lifecycle State Machine & Reducer

## Canonical states

```
proposed → comment_open → comment_closed → final_pending_effective → effective
   └──────────────┴──────────────┴──────────── withdrawn / stalled / superseded
```

Forward transitions and jumps forward are legal (a source may first be seen at any stage).
Backward transitions are **anomalies** except:
- `comment_closed → comment_open` (comment period reopened/extended — legal, common),
- `stalled → any forward state` (item resumed),
- `final_pending_effective → comment_open` (rule re-proposed — rare; legal + anomaly note).

Illegal transitions: record `anomaly` event, keep the stored state, surface in health UI.

## Reducer algorithm (per NormalizedItem)

1. Resolve identity via join keys. No match → create `pipeline_item`, insert all join keys,
   insert all dates as key_dates, emit `item_created` (+ initial `state_changed` from null).
2. Match → for each incoming NormalizedDate, compare against current key_date for
   (date_type, label):
   - none exists → insert, emit `date_changed` {from: null}
   - exists with different value → insert new row with supersedes_id, emit `date_changed`
   - equal → no-op (do NOT re-assert; keeps table minimal)
3. Compute target state from statemap (source native_status + date context vs today).
   If different from stored: legal → update + `state_changed`; illegal → `anomaly`.
4. `content_fingerprint` differs → `content_changed` (payload includes old/new fingerprint).
5. Merge any new join keys (e.g. docket id learned later); conflict (key already bound to a
   different item) → `anomaly`, no merge.
6. Update `last_seen_at`.

## Statemap (data-driven, per source)

Mapping from native status + date predicates → canonical state, e.g. Federal Register:

| native | predicate | state |
|---|---|---|
| PRORULE | comment_close ≥ today | comment_open |
| PRORULE | comment_close < today | comment_closed |
| PRORULE | no comment_close | proposed |
| RULE | effective > today | final_pending_effective |
| RULE | effective ≤ today or absent | effective |

**Time-based transitions** (comment window closing, effective date passing) are computed by
a daily `tick` job that re-evaluates state from stored dates — no fetch required — and emits
`state_changed` accordingly.

## Linker

After each run: for any `RULE` item sharing a `rin` join key with a `PRORULE` item, set the
proposed item's `resolved_change_id` → final item id, state → `superseded` is WRONG — the
proposed item transitions to `final_pending_effective`/`effective` is also wrong (they are
distinct documents). Correct behavior: mark proposed item resolved (`item_resolved` event,
`resolved_change_id` set); its state becomes `superseded` by the final document. The final
document remains the live item carrying effective dates.
