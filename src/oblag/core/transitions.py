from __future__ import annotations

import enum

from oblag.db.models import ItemState as S

_RANK: dict[S, int] = {
    S.proposed: 0,
    S.comment_open: 1,
    S.comment_closed: 2,
    S.final_pending_effective: 3,
    S.effective: 4,
}
_TERMINAL = {S.withdrawn, S.superseded}
_SIDE = {S.withdrawn, S.stalled, S.superseded}

# Legal backward exceptions on the main track (spec 03)
_BACKWARD_OK = {
    (S.comment_closed, S.comment_open),  # comment period reopened / extended
    (S.final_pending_effective, S.comment_open),  # rule re-proposed (rare)
}


class Verdict(enum.Enum):
    ok = "ok"
    noop = "noop"
    anomaly = "anomaly"


def classify_transition(current: S, new: S) -> Verdict:
    if current == new:
        return Verdict.noop
    if current is S.superseded and new in _RANK:
        # A superseded item's source document usually stays in its feed for months;
        # re-reads keep deriving the old mainline state. That's a stale echo of a
        # resolved item, not a contradiction — anomaly spam would drown real ones.
        return Verdict.noop
    if current in _TERMINAL:
        return Verdict.anomaly
    if new in _SIDE:
        return Verdict.ok
    if current is S.stalled:
        return Verdict.ok  # resumed at any stage
    if (current, new) in _BACKWARD_OK:
        return Verdict.ok
    if _RANK[new] > _RANK[current]:
        return Verdict.ok
    return Verdict.anomaly
