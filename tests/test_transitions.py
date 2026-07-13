from __future__ import annotations

import pytest

from oblag.core.transitions import Verdict, classify_transition
from oblag.db.models import ItemState as S


@pytest.mark.parametrize(
    ("cur", "new", "verdict"),
    [
        # forward moves and jumps are legal (spec 03)
        (S.proposed, S.comment_open, Verdict.ok),
        (S.proposed, S.effective, Verdict.ok),
        (S.comment_open, S.comment_closed, Verdict.ok),
        (S.comment_closed, S.final_pending_effective, Verdict.ok),
        (S.final_pending_effective, S.effective, Verdict.ok),
        # no-op
        (S.comment_open, S.comment_open, Verdict.noop),
        # side states reachable from anywhere active
        (S.comment_open, S.withdrawn, Verdict.ok),
        (S.proposed, S.stalled, Verdict.ok),
        (S.comment_closed, S.superseded, Verdict.ok),
        # legal backward exceptions
        (S.comment_closed, S.comment_open, Verdict.ok),  # comment period reopened/extended
        (S.stalled, S.comment_closed, Verdict.ok),  # resumed
        (S.final_pending_effective, S.comment_open, Verdict.ok),  # re-proposed (rare)
        # illegal
        (S.effective, S.comment_open, Verdict.anomaly),
        (S.comment_closed, S.proposed, Verdict.anomaly),
        (S.effective, S.proposed, Verdict.anomaly),
        # terminal states never transition
        (S.withdrawn, S.comment_open, Verdict.anomaly),
        (S.superseded, S.effective, Verdict.anomaly),
        (S.withdrawn, S.stalled, Verdict.anomaly),
    ],
)
def test_transition_matrix(cur, new, verdict):
    assert classify_transition(cur, new) is verdict
