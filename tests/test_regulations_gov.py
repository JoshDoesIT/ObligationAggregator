from __future__ import annotations

import json
from datetime import date

import pytest

from conftest import load_fixture
from oblag.adapters.base import NormalizedDate, NormalizedItem, RawDocument
from oblag.adapters.regulations_gov import RegulationsGovAdapter, _eastern_date
from oblag.core.linker import link_resolved_items
from oblag.core.reducer import current_dates, reduce_item
from oblag.db.models import Confidence, DateType, EventType, ItemState, PipelineItem


@pytest.fixture()
def adapter(monkeypatch):
    monkeypatch.setenv("OBLAG_REGSGOV_API_KEY", "DEMO_KEY")
    from oblag.config import get_settings

    get_settings.cache_clear()
    return RegulationsGovAdapter()


def _docket_info():
    body = json.loads(load_fixture("regulations_gov", "docket_detail.json"))
    attrs = body["data"]["attributes"]
    return {
        "CISA-2022-0010": {
            "rin": attrs["rin"],
            "docketType": attrs["docketType"],
            "title": attrs["title"],
        }
    }


def _page(adapter):
    raw = RawDocument(
        url="https://test",
        content=load_fixture("regulations_gov", "documents_page.json"),
        meta={"docket_info": _docket_info()},
    )
    return list(adapter.normalize(raw))


def test_disabled_without_api_key(monkeypatch):
    monkeypatch.delenv("OBLAG_REGSGOV_API_KEY", raising=False)
    from oblag.config import get_settings

    get_settings.cache_clear()
    assert RegulationsGovAdapter().enabled() is False


def test_eastern_deadline_conversion():
    # 2024-07-04T03:59:59Z is 11:59:59 PM ET on July 3 — the civil deadline date
    assert _eastern_date("2024-07-04T03:59:59Z") == date(2024, 7, 3)
    assert _eastern_date(None) is None
    assert _eastern_date("garbage") is None


def test_normalize_real_fixture(adapter):
    items = {i.external_key[1]: i for i in _page(adapter)}
    nprm = items["CISA-2022-0010-0163"]
    assert ("fr_doc_number", "2024-06526") in nprm.join_keys
    assert ("docket_id", "CISA-2022-0010") in nprm.join_keys
    # live docket carries rin="Not Assigned" — the sentinel must NOT become a join key
    assert not any(k[0] == "rin" for k in nprm.join_keys)
    assert (DateType.comment_close, date(2024, 7, 3)) in {
        (d.date_type, d.value) for d in nprm.dates
    }


def test_enrichment_merges_into_fr_item_without_state_regression(db, adapter):
    # FR item, comment window closed long ago
    fr = reduce_item(
        db,
        NormalizedItem(
            source_system="federal_register",
            external_key=("fr_doc_number", "2024-06526"),
            jurisdiction="US-Federal",
            title="CIRCIA NPRM",
            native_status="PRORULE",
            track="proposed",
            join_keys=[("docket_id", "CISA-2022-0010")],
            dates=[
                NormalizedDate(DateType.comment_close, date(2024, 7, 3), Confidence.published_firm)
            ],
        ),
        today=date(2026, 7, 1),
    ).item
    assert fr.state is ItemState.comment_closed

    events = []
    for ni in _page(adapter):
        events.extend(reduce_item(db, ni, today=date(2026, 7, 1)).events)
    # all five regs.gov PRORULE docs share the docket → merged into the FR item
    proposed_items = db.query(PipelineItem).filter_by(track="proposed").count()
    assert proposed_items == 1
    db.refresh(fr)
    assert fr.state is ItemState.comment_closed  # no regression from date-less docs
    assert not [e for e in events if e.type is EventType.anomaly]
    # regs.gov doc ids joined for cross-source correlation
    key_types = {k.type for k in fr.join_keys}
    assert "regsgov_doc" in key_types
    # comment_close was NOT changed (2024-07-04T03:59:59Z → same 2024-07-03 civil date)
    assert current_dates(db, fr.id)[(DateType.comment_close, None)].value == date(2024, 7, 3)


def test_resolved_item_reappearing_is_not_anomalous(db):
    proposed = reduce_item(
        db,
        NormalizedItem(
            source_system="federal_register",
            external_key=("fr_doc_number", "2024-1"),
            jurisdiction="US-Federal",
            title="NPRM",
            native_status="PRORULE",
            track="proposed",
            join_keys=[("rin", "1111-AA11")],
        ),
        today=date(2026, 1, 1),
    ).item
    reduce_item(
        db,
        NormalizedItem(
            source_system="federal_register",
            external_key=("fr_doc_number", "2026-2"),
            jurisdiction="US-Federal",
            title="Final",
            native_status="RULE",
            track="final",
            join_keys=[("rin", "1111-AA11")],
        ),
        today=date(2026, 1, 1),
    )
    link_resolved_items(db)
    db.refresh(proposed)
    assert proposed.state is ItemState.superseded
    # the old NPRM shows up again in a later fetch window
    res = reduce_item(
        db,
        NormalizedItem(
            source_system="federal_register",
            external_key=("fr_doc_number", "2024-1"),
            jurisdiction="US-Federal",
            title="NPRM",
            native_status="PRORULE",
            track="proposed",
            join_keys=[("rin", "1111-AA11")],
        ),
        today=date(2026, 2, 1),
    )
    assert not [e for e in res.events if e.type is EventType.anomaly]
    assert res.item.state is ItemState.superseded
