from __future__ import annotations

from datetime import date

import pytest

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.nist_csrc import NistCsrcAdapter, _split_stage
from oblag.core.reducer import reduce_item
from oblag.db.models import DateType, EventType, ItemState


@pytest.fixture()
def items():
    adapter = NistCsrcAdapter()
    raw = RawDocument(url="https://test", content=load_fixture("nist_csrc", "feed.json"))
    return {i.external_key[1]: i for i in adapter.normalize(raw)}


def test_stage_split_known_and_unknown():
    assert _split_stage("https://csrc.nist.gov/pubs/sp/800/219/r2/ipd") == (
        "https://csrc.nist.gov/pubs/sp/800/219/r2",
        "ipd",
    )
    assert _split_stage("https://csrc.nist.gov/pubs/sp/800/73/pt1/6/iwd") == (
        "https://csrc.nist.gov/pubs/sp/800/73/pt1/6",
        "iwd",
    )
    assert _split_stage("https://csrc.nist.gov/pubs/sp/800/38/d/r1/2prd") == (
        "https://csrc.nist.gov/pubs/sp/800/38/d/r1",
        "2prd",
    )
    # unknown suffix → identity preserved, no stage claimed
    assert _split_stage("https://csrc.nist.gov/pubs/sp/800/999/zzz")[1] is None


def test_normalize_live_feed_entries(items):
    # IR 8320E ipd with a real due date
    ipd = items["https://csrc.nist.gov/pubs/ir/8320/e"]
    assert ipd.native_status == "ipd"
    assert (DateType.comment_close, date(2026, 7, 13)) in {
        (d.date_type, d.value) for d in ipd.dates
    }
    # stage phrase stripped from concatenated title
    assert not ipd.title.endswith("Initial Public Draft")

    # iwd with "No Due Date: Comment Period Remains Open" → no comment_close, no anomaly
    iwd = items["https://csrc.nist.gov/pubs/sp/800/73/pt1/6"]
    assert iwd.native_status == "iwd"
    assert DateType.comment_close not in {d.date_type for d in iwd.dates}
    assert iwd.anomalies == []

    # unknown stage suffix → anomaly note, still ingested as draft
    weird = items["https://csrc.nist.gov/pubs/sp/800/999/zzz"]
    assert weird.native_status == "unknown"
    assert any("unknown draft-stage" in a for a in weird.anomalies)


def test_stage_progression_same_identity(db, items):
    """ipd → 2pd on the same base URL is ONE item with a state/content change."""
    ipd = items["https://csrc.nist.gov/pubs/ir/8320/e"]
    res = reduce_item(db, ipd, today=date(2026, 6, 1))
    assert res.created
    assert res.item.state is ItemState.comment_open

    import dataclasses

    twopd = dataclasses.replace(
        ipd,
        native_status="2pd",
        url="https://csrc.nist.gov/pubs/ir/8320/e/2pd",
        native_meta={"stage_name": "Second Public Draft", "feed_link": ""},
    )
    res2 = reduce_item(db, twopd, today=date(2026, 6, 2))
    assert not res2.created  # same publication, new draft stage
    assert EventType.content_changed in {e.type for e in res2.events}
    assert res2.item.native_status == "2pd"


def test_no_due_date_item_stays_open_after_tick(db, items):
    from oblag.core.reducer import tick

    iwd = items["https://csrc.nist.gov/pubs/sp/800/73/pt1/6"]
    reduce_item(db, iwd, today=date(2026, 6, 13))
    assert tick(db, today=date(2027, 1, 1)) == []  # open-ended comment period stays open
