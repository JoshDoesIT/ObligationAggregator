"""M1 acceptance benchmark (spec: DEVELOPMENT_PLAN.md): reconstruct the CIRCIA
lifecycle (RIN 1670-AA04) from recorded Federal Register API responses.

Expected signal stream:
  2024-04-04  NPRM published            → item_created, state comment_open
  2024-05-06  comment period extended   → date_changed comment_close 06-03 → 07-03
  2024-06-03  correction published      → NO date regression (bogus metadata dropped)
  2024-07-04  window passes (tick)      → state_changed comment_open → comment_closed
  curated     projected final slips     → date_changed projected_final, statutory → estimate
  no false `effective` at any point
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import respx
from httpx import Response

from conftest import load_fixture
from oblag.core.assertions import assert_date
from oblag.core.reducer import current_dates, tick
from oblag.core.runner import run_adapter
from oblag.db.models import (
    Confidence,
    DateType,
    Event,
    EventType,
    ItemState,
    PipelineItem,
)

EMPTY_PAGE = {"count": 0, "total_pages": 0, "next_page_url": None, "results": []}


def _mock_fr(respx_mock, prorule_fixture: str) -> None:
    def route(request):
        params = dict(request.url.params)
        if params.get("conditions[type][]") == "PRORULE":
            return Response(200, content=load_fixture("federal_register", prorule_fixture))
        return Response(200, json=EMPTY_PAGE)

    respx_mock.get(url__startswith="https://www.federalregister.gov/api/v1/documents").mock(
        side_effect=route
    )


def run_day(db, fixture: str, day: date):
    with respx.mock(assert_all_called=False) as respx_mock:
        _mock_fr(respx_mock, fixture)
        return run_adapter(
            db,
            "federal_register",
            since=datetime(2024, 1, 1, tzinfo=UTC),
            today=day,
        )


def test_circia_lifecycle_reconstruction(db):
    # Day 1 — NPRM published
    stats = run_day(db, "circia_page1_nprm.json", date(2024, 4, 5))
    assert stats.errors == []
    assert stats.created == 1
    item = db.query(PipelineItem).one()
    assert item.state is ItemState.comment_open
    assert {e.type.value for e in stats.events} == {"item_created", "state_changed"}

    # Day 2 — extension notice moves comment_close (a DIFFERENT FR document, same RIN/docket)
    stats = run_day(db, "circia_page2_extension.json", date(2024, 5, 7))
    date_events = [e for e in stats.events if e.type is EventType.date_changed]
    assert len(date_events) == 1
    assert date_events[0].payload["date_type"] == "comment_close"
    assert date_events[0].payload["from"] == "2024-06-03"
    assert date_events[0].payload["to"] == "2024-07-03"
    assert db.query(PipelineItem).count() == 1  # matched, not duplicated

    # Day 3 — correction document must NOT regress the comment_close date
    stats = run_day(db, "circia_page3_correction.json", date(2024, 6, 4))
    assert [e for e in stats.events if e.type is EventType.date_changed] == []
    live = current_dates(db, item.id)
    assert live[(DateType.comment_close, None)].value == date(2024, 7, 3)
    # the dropped bogus date is visible as an adapter_parse anomaly, not silence
    anomalies = [e for e in stats.events if e.type is EventType.anomaly]
    assert any("comments_close_on" in a.payload.get("detail", "") for a in anomalies)
    # comment window still open on 2024-06-04 (would have closed under the bogus date!)
    db.refresh(item)
    assert item.state is ItemState.comment_open

    # Day 4 — no fetch needed: the tick closes the window when the date passes
    events = tick(db, today=date(2024, 7, 4))
    assert [e.payload for e in events] == [{"from": "comment_open", "to": "comment_closed"}]

    # Curated projected-final tracking (Unified Agenda has no API; spec: assertions):
    # statutory 18-month deadline ≈ Oct 2025, later slipped to May 2026 per reporting.
    ev = assert_date(
        db,
        item.id,
        DateType.projected_final,
        date(2025, 10, 4),
        Confidence.statutory_hard,
        note="CIRCIA statutory 18-month final-rule deadline",
    )
    assert ev is not None and ev.payload["from"] is None
    ev = assert_date(
        db,
        item.id,
        DateType.projected_final,
        date(2026, 5, 1),
        Confidence.agency_estimate,
        note="Sept 2025 regulatory filing revised the statutory deadline",
    )
    assert ev is not None
    assert ev.payload["from"] == "2025-10-04"
    assert ev.payload["to"] == "2026-05-01"
    assert ev.payload["confidence"] == "agency_estimate"

    # Full audit trail: supersession chain intact, and never a false `effective`
    chain = (
        db.query(Event)
        .filter(Event.pipeline_item_id == item.id, Event.type == EventType.state_changed)
        .order_by(Event.id)
        .all()
    )
    assert [e.payload["to"] for e in chain] == ["comment_open", "comment_closed"]
    db.refresh(item)
    assert item.state is ItemState.comment_closed
    assert (DateType.effective, None) not in current_dates(db, item.id)
