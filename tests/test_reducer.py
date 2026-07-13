from __future__ import annotations

from datetime import date

from oblag.adapters.base import NormalizedDate, NormalizedItem
from oblag.core.reducer import current_dates, reduce_item, tick
from oblag.db.models import (
    Confidence,
    DateType,
    EventType,
    ItemState,
    KeyDate,
    PipelineItem,
)

TODAY = date(2024, 5, 1)


def nprm(comment_close: date | None = date(2024, 6, 3), **kw) -> NormalizedItem:
    dates = [NormalizedDate(DateType.proposal_date, date(2024, 4, 4), Confidence.published_firm)]
    if comment_close:
        dates.append(
            NormalizedDate(DateType.comment_close, comment_close, Confidence.published_firm)
        )
    defaults = dict(
        source_system="federal_register",
        external_key=("fr_doc_number", "2024-06526"),
        jurisdiction="US-Federal",
        title="CIRCIA Reporting Requirements",
        native_status="PRORULE",
        track="proposed",
        join_keys=[("rin", "1670-AA04"), ("docket_id", "CISA-2022-0010")],
        dates=dates,
    )
    defaults.update(kw)
    return NormalizedItem(**defaults)


def types_of(events) -> list[str]:
    return [e.type.value for e in events]


def test_new_item_created_with_dates_and_state(db):
    res = reduce_item(db, nprm(), today=TODAY)
    assert res.created
    assert types_of(res.events) == ["item_created", "state_changed"]
    assert res.events[1].payload == {"from": None, "to": "comment_open"}
    item = res.item
    assert item.state is ItemState.comment_open
    assert {(k.type, k.value) for k in item.join_keys} == {
        ("fr_doc_number", "2024-06526"),
        ("rin", "1670-AA04"),
        ("docket_id", "CISA-2022-0010"),
    }
    assert len(item.key_dates) == 2


def test_rerun_same_item_is_silent(db):
    reduce_item(db, nprm(), today=TODAY)
    res = reduce_item(db, nprm(), today=TODAY)
    assert not res.created
    assert res.events == []
    assert db.query(KeyDate).count() == 2  # no re-assertion


def test_date_change_supersedes_and_emits(db):
    reduce_item(db, nprm(), today=TODAY)
    # comment period extended: same item matched via rin/docket from a NEW FR document
    extension = nprm(
        comment_close=date(2024, 7, 3),
        external_key=("fr_doc_number", "2024-09689"),
        native_meta={"action": "Proposed rule; extension of comment period"},
    )
    res = reduce_item(db, extension, today=TODAY)
    assert not res.created
    changed = [e for e in res.events if e.type is EventType.date_changed]
    assert len(changed) == 1
    assert changed[0].payload["date_type"] == "comment_close"
    assert changed[0].payload["from"] == "2024-06-03"
    assert changed[0].payload["to"] == "2024-07-03"
    # append-only chain
    rows = (
        db.query(KeyDate)
        .filter_by(pipeline_item_id=res.item.id, date_type=DateType.comment_close)
        .all()
    )
    assert len(rows) == 2
    cur = current_dates(db, res.item.id)
    assert cur[(DateType.comment_close, None)].value == date(2024, 7, 3)
    # the new fr_doc_number was merged as an additional join key
    assert ("fr_doc_number", "2024-09689") in {(k.type, k.value) for k in res.item.join_keys}


def test_tick_closes_comment_window_without_fetch(db):
    res = reduce_item(db, nprm(), today=TODAY)
    assert res.item.state is ItemState.comment_open
    events = tick(db, today=date(2024, 6, 4))
    assert [e.type.value for e in events] == ["state_changed"]
    assert events[0].payload == {"from": "comment_open", "to": "comment_closed"}
    db.refresh(res.item)
    assert res.item.state is ItemState.comment_closed


def test_illegal_transition_records_anomaly_keeps_state(db):
    reduce_item(db, nprm(), today=TODAY)
    withdrawal = nprm(
        external_key=("fr_doc_number", "2024-55555"),
        native_meta={"action": "Proposed rule; withdrawal"},
    )
    res = reduce_item(db, withdrawal, today=TODAY)
    assert res.item.state is ItemState.withdrawn
    # a stale feed page re-serves the original NPRM as active: withdrawn is terminal → anomaly
    stale = nprm(native_meta={"action": "Proposed rule."})
    res2 = reduce_item(db, stale, today=TODAY)
    anomalies = [e for e in res2.events if e.type is EventType.anomaly]
    assert res2.item.state is ItemState.withdrawn  # unchanged, terminal
    assert len(anomalies) == 1
    assert anomalies[0].payload["kind"] == "illegal_transition"


def test_content_change_emits_event(db):
    reduce_item(db, nprm(), today=TODAY)
    res = reduce_item(db, nprm(title="CIRCIA Reporting Requirements; Correction"), today=TODAY)
    assert EventType.content_changed in {e.type for e in res.events}


def test_track_separation_prevents_final_merging_into_proposed(db):
    reduce_item(db, nprm(), today=TODAY)
    final = nprm(
        external_key=("fr_doc_number", "2025-99999"),
        native_status="RULE",
        track="final",
        comment_close=None,
    )
    final.dates = [NormalizedDate(DateType.effective, date(2026, 1, 1), Confidence.published_firm)]
    res = reduce_item(db, final, today=TODAY)
    assert res.created  # distinct item despite shared rin/docket join keys
    assert res.item.state is ItemState.final_pending_effective
    assert db.query(PipelineItem).count() == 2


def test_join_key_conflict_same_track_is_anomaly(db):
    reduce_item(db, nprm(), today=TODAY)
    # a different proposed item claims the same docket
    other = nprm(
        external_key=("fr_doc_number", "2024-11111"),
        title="Unrelated NPRM",
        join_keys=[("docket_id", "CISA-2022-0010")],
    )
    other.dates = []
    res = reduce_item(db, other, today=TODAY)
    # matched into the existing item by docket — that is by design; but if it had
    # matched TWO items we'd get an anomaly. Simulate that:
    third = nprm(
        external_key=("fr_doc_number", "2024-22222"),
        title="Another NPRM",
        join_keys=[("rin", "9999-ZZ99")],
    )
    reduce_item(db, third, today=TODAY)
    ambiguous = nprm(
        external_key=("fr_doc_number", "2024-33333"),
        join_keys=[("rin", "1670-AA04"), ("rin", "9999-ZZ99")],
    )
    res = reduce_item(db, ambiguous, today=TODAY)
    assert EventType.anomaly in {e.type for e in res.events}
