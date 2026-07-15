from __future__ import annotations

from datetime import date

import pytest

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.federal_register import FederalRegisterAdapter
from oblag.db.models import Confidence, DateType


@pytest.fixture()
def adapter() -> FederalRegisterAdapter:
    return FederalRegisterAdapter()


def normalize_fixture(adapter, name):
    raw = RawDocument(url="https://test", content=load_fixture("federal_register", name))
    return list(adapter.normalize(raw))


def test_normalize_nprm(adapter):
    items = normalize_fixture(adapter, "circia_page1_nprm.json")
    assert len(items) == 1
    item = items[0]
    assert item.external_key == ("fr_doc_number", "2024-06526")
    assert item.native_status == "PRORULE"
    assert item.track == "proposed"
    assert ("rin", "1670-AA04") in item.join_keys
    assert ("docket_id", "CISA-2022-0010") in item.join_keys
    date_map = {(d.date_type, d.value) for d in item.dates}
    assert (DateType.comment_close, date(2024, 6, 3)) in date_map
    assert (DateType.proposal_date, date(2024, 4, 4)) in date_map
    assert all(d.confidence is Confidence.published_firm for d in item.dates)


def test_normalize_mixed_page_filters_and_maps(adapter):
    items = normalize_fixture(adapter, "mixed_page.json")
    by_key = {i.external_key[1]: i for i in items}
    # ANPRM excluded by default (weak signal)
    assert "2099-00001" not in by_key
    # withdrawal kept, action preserved for statemap
    assert "withdrawal" in by_key["2099-00002"].native_meta["action"].lower()
    # the real RULE mapped to final track with adopted+effective dates
    rule = next(i for i in items if i.native_status == "RULE")
    assert rule.track == "final"
    assert {d.date_type for d in rule.dates} >= {DateType.adopted}


def test_anprm_included_when_prerule_enabled(adapter, monkeypatch):
    monkeypatch.setenv("OBLAG_INCLUDE_PRERULE", "true")
    from oblag.config import get_settings

    get_settings.cache_clear()
    items = normalize_fixture(adapter, "mixed_page.json")
    assert any(i.external_key[1] == "2099-00001" for i in items)


def test_correction_documents_do_not_move_dates(adapter):
    items = normalize_fixture(adapter, "circia_page3_correction.json")
    assert len(items) == 1
    item = items[0]
    # bogus comments_close_on=2024-04-04 on the correction must be dropped + noted
    assert DateType.comment_close not in {d.date_type for d in item.dates}
    assert any("comments_close_on" in a for a in item.anomalies)


def test_malformed_page_yields_nothing(adapter):
    raw = RawDocument(url="https://test", content=b"not json{{")
    assert list(adapter.normalize(raw)) == []
    raw2 = RawDocument(url="https://test", content=b'{"results": [{"type": "Notice"}]}')
    assert list(adapter.normalize(raw2)) == []
