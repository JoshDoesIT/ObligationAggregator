from __future__ import annotations

from datetime import date

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.have_your_say import HaveYourSayAdapter
from oblag.core.reducer import reduce_item, tick
from oblag.db.models import DateType, ItemState


def _items():
    adapter = HaveYourSayAdapter()
    raw = RawDocument(
        url="https://test",
        content=load_fixture("have_your_say", "digital_page0.json"),
        meta={"topic": "DIGITAL"},
    )
    return {i.external_key[1]: i for i in adapter.normalize(raw)}


def test_planning_only_entries_are_excluded():
    items = _items()
    # INIT_PLANNED/DISABLED entries (no feedback window) are weak signals
    assert "16072" not in items  # e-signature validation, planning only
    assert "14628" in items  # Cloud and AI Development Act, feedback open


def test_open_feedback_window_normalized(db):
    items = _items()
    cloud_ai = items["14628"]
    assert cloud_ai.title == "Cloud and AI Development Act"
    assert cloud_ai.native_meta["foreseen_act_type"] == "PROP_REG"
    dates = {d.date_type: d.value for d in cloud_ai.dates}
    assert dates[DateType.comment_close] == date(2026, 9, 7)

    res = reduce_item(db, cloud_ai, today=date(2026, 7, 14))
    assert res.item.state is ItemState.comment_open
    events = tick(db, today=date(2026, 9, 8))
    assert [e.payload["to"] for e in events] == ["comment_closed"]


def test_closed_feedback_window_state(db):
    items = _items()
    cyber = items["14578"]  # EU Cybersecurity Act, feedback closed 2026-05-12
    res = reduce_item(db, cyber, today=date(2026, 7, 14))
    assert res.item.state is ItemState.comment_closed


def test_float_ids_are_canonicalized():
    items = _items()
    assert all(iid == str(int(float(iid))) for iid in items)
