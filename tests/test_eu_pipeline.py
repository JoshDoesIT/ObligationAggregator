from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.cellar import CellarAdapter
from oblag.adapters.oeil import OeilAdapter
from oblag.core.reducer import reduce_item, tick
from oblag.db.models import DateType, EventType, ItemState


@pytest.fixture()
def acts():
    adapter = CellarAdapter()
    raw = RawDocument(
        url="https://sparql",
        content=load_fixture("cellar", "acts_aiact_window.json"),
        meta={"kind": "acts"},
    )
    return {i.external_key[1]: i for i in adapter.normalize(raw)}


@pytest.fixture()
def proposals():
    adapter = CellarAdapter()
    raw = RawDocument(
        url="https://sparql",
        content=load_fixture("cellar", "proposals_window.json"),
        meta={"kind": "proposals"},
    )
    return list(adapter.normalize(raw))


def test_ai_act_reference_multistage_case(acts):
    """The plan's reference case: AI Act phased application must be fully modeled."""
    ai = acts["32024R1689"]
    assert ai.native_status == "REG"
    assert ai.track == "final"
    by_type = {}
    for d in ai.dates:
        by_type.setdefault(d.date_type, []).append((d.label, d.value))
    assert by_type[DateType.adopted] == [(None, date(2024, 6, 13))]
    assert by_type[DateType.entry_into_force] == [(None, date(2024, 8, 1))]
    phased = sorted(by_type[DateType.phased_compliance])
    assert [v for _, v in phased] == [
        date(2025, 2, 2),
        date(2025, 8, 2),
        date(2026, 8, 2),
        date(2027, 8, 2),
    ]
    assert len(by_type[DateType.transition_deadline]) == 7
    assert "2024/1689" in ai.title


def test_proposals_normalize(proposals):
    assert len(proposals) >= 10
    p = proposals[0]
    assert p.track == "proposed"
    assert p.native_status.startswith("PROP_")
    assert p.external_key[0] == "celex"
    assert any(d.date_type is DateType.proposal_date for d in p.dates)


def test_cellar_states_and_tick(db, acts):
    ai = acts["32024R1689"]
    res = reduce_item(db, ai, today=date(2024, 7, 1))
    assert res.item.state is ItemState.final_pending_effective  # EIF 2024-08-01 in future
    events = tick(db, today=date(2024, 8, 2))
    assert [e.payload for e in events] == [{"from": "final_pending_effective", "to": "effective"}]


def test_digital_omnibus_style_date_shift_emits_event(db, acts):
    """A later amendment moving a phased application date → date_changed on that label."""
    ai = acts["32024R1689"]
    reduce_item(db, ai, today=date(2024, 7, 1))
    shifted_dates = [
        dataclasses.replace(d, value=date(2027, 12, 2))
        if d.date_type is DateType.phased_compliance and d.value == date(2026, 8, 2)
        else d
        for d in ai.dates
    ]
    shifted = dataclasses.replace(ai, dates=shifted_dates)
    res = reduce_item(db, shifted, today=date(2026, 5, 8))
    changed = [e for e in res.events if e.type is EventType.date_changed]
    assert len(changed) == 1
    assert changed[0].payload["from"] == "2026-08-02"
    assert changed[0].payload["to"] == "2027-12-02"
    assert changed[0].payload["date_type"] == "phased_compliance"
    # append-only history: both values remain queryable
    item = res.item
    label = changed[0].payload["label"]
    rows = [
        kd
        for kd in item.key_dates
        if kd.date_type is DateType.phased_compliance and kd.label == label
    ]
    assert len(rows) == 2


def test_corrigendum_is_anomaly_note_on_base_act(db, acts):
    adapter = CellarAdapter()
    raw = RawDocument(
        url="https://sparql",
        content=load_fixture("cellar", "acts_aiact_window.json"),
        meta={"kind": "acts"},
    )
    items = list(adapter.normalize(raw))
    corr = [i for i in items if i.anomalies and "corrigendum" in i.anomalies[0]]
    if not corr:  # fixture window may contain no corrigenda; construct one
        import json

        body = json.loads(load_fixture("cellar", "acts_aiact_window.json"))
        row = dict(body["results"]["bindings"][0])
        row["celex"] = {"type": "literal", "value": "32024R1689R(01)"}
        item = adapter._normalize_row(row, "acts")
        corr = [item]
    c = corr[0]
    assert c.external_key == ("celex", c.external_key[1])
    assert "R(" not in c.external_key[1]  # identity is the BASE act
    assert any("corrigendum" in a for a in c.anomalies)


def test_oeil_watched_procedure_parse(db):
    adapter = OeilAdapter()
    raw = RawDocument(
        url="https://oeil.europarl.europa.eu/oeil/en/procedure-file?reference=2021/0106(COD)",
        content=load_fixture("oeil", "procedure_2021_0106.html"),
        content_type="text/html",
        meta={"reference": "2021/0106(COD)"},
    )
    items = list(adapter.normalize(raw))
    assert len(items) == 1
    item = items[0]
    assert item.external_key == ("oeil_procedure", "2021/0106(COD)")
    assert item.native_status == "Procedure completed"
    assert item.anomalies == []
    res = reduce_item(db, item, today=date(2026, 7, 14))
    assert res.item.state is ItemState.effective

    # stage progression: a fresh procedure at an 'awaiting' stage maps to proposed;
    # an unknown stage string is an anomaly, never a crash (open enum)
    fresh = dataclasses.replace(
        item,
        external_key=("oeil_procedure", "2026/0001(COD)"),
        native_status="Awaiting committee decision",
    )
    assert reduce_item(db, fresh, today=date(2026, 7, 14)).item.state is ItemState.proposed
    weird = dataclasses.replace(
        item,
        external_key=("oeil_procedure", "2026/0002(COD)"),
        native_status="Some brand new stage",
    )
    res = reduce_item(db, weird, today=date(2026, 7, 14))
    assert EventType.anomaly in {e.type for e in res.events}


def test_oeil_disabled_without_watched_procedures():
    assert OeilAdapter().enabled() is False
