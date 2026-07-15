from __future__ import annotations

from datetime import date

import pytest
import respx
from httpx import Response

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.legiscan import LegiscanAdapter
from oblag.core.assertions import assert_date
from oblag.core.reducer import reduce_item
from oblag.db.models import Confidence, DateType, ItemState

# --- LegiScan ---


def _bill(fixture: str):
    adapter = LegiscanAdapter()
    raw = RawDocument(
        url="https://api.legiscan.com/?op=getBill",
        content=load_fixture("legiscan", fixture),
        meta={"kind": "bill"},
    )
    return list(adapter.normalize(raw))


def test_legiscan_passed_bill_becomes_pending_effective(db):
    (item,) = _bill("bill_passed.json")
    assert item.jurisdiction == "US-RI"
    assert item.track == "final"
    assert ("bill_id", "RI-H7787") in item.join_keys
    res = reduce_item(db, item, today=date(2024, 7, 1))
    # passed but no effective date known yet → pending, awaiting curated assertion
    assert res.item.state is ItemState.final_pending_effective

    # curated workflow: effective date asserted with citation (IAPP cross-check)
    assert_date(
        db,
        res.item.id,
        DateType.effective,
        date(2026, 1, 1),
        Confidence.published_firm,
        note="RIGL 6-48.1; effective Jan 1 2026",
    )
    from oblag.core.reducer import tick

    events = tick(db, today=date(2026, 1, 2))
    assert [e.payload["to"] for e in events] == ["effective"]


def test_legiscan_weak_signals_never_enter_pipeline():
    assert _bill("bill_introduced.json") == []


def test_legiscan_disabled_without_key_and_states(monkeypatch):
    monkeypatch.delenv("OBLAG_LEGISCAN_API_KEY", raising=False)
    from oblag.config import get_settings

    get_settings.cache_clear()
    assert LegiscanAdapter().enabled() is False


# --- OSCAL export ---


def test_oscal_export_shape_and_stability(db, seeded):
    from oblag.oscal import export_catalog

    doc = export_catalog(db)
    catalog = doc["catalog"]
    assert catalog["metadata"]["oscal-version"] == "1.1.2"
    resources = catalog["back-matter"]["resources"]
    assert len(resources) == 1
    res = resources[0]
    props = {p["name"]: p for p in res["props"]}
    assert props["state"]["value"] == "comment_open"
    assert props["join-key-rin"]["value"] == "1670-AA04"
    date_props = [p for name, p in props.items() if name.startswith("date-comment-close")]
    assert date_props and date_props[0]["class"] == "published_firm"
    # deterministic UUIDs: same export twice → same uuids
    assert export_catalog(db)["catalog"]["back-matter"]["resources"][0]["uuid"] == res["uuid"]
    with pytest.raises(ValueError):
        export_catalog(db, "nonexistent")


def test_oscal_export_api(client, seeded):
    r = client.get("/api/v1/export/oscal")
    assert r.status_code == 200
    assert "catalog" in r.json()
    assert client.get("/api/v1/export/oscal?obligation=nope").status_code == 404


# --- severity classification ---


def test_event_severity_in_api(client, seeded):
    events = client.get("/api/v1/events").json()["events"]
    by_type = {e["type"]: e["severity"] for e in events}
    assert by_type["item_created"] == "new_obligation"
    assert by_type["state_changed"] == "substantive"


# --- AI assist (mocked provider; off by default) ---


def test_ai_off_by_default(db, seeded):
    from oblag.ai import AiNotConfigured, summarize_item

    with pytest.raises(AiNotConfigured):
        summarize_item(db, 1)


def test_ai_draft_includes_disclaimer_and_citations(db, seeded, monkeypatch):
    monkeypatch.setenv("OBLAG_AI_PROVIDER", "anthropic")
    monkeypatch.setenv("OBLAG_AI_API_KEY", "test-key")
    from oblag.config import get_settings

    get_settings.cache_clear()
    from oblag.ai import summarize_item
    from oblag.db.models import PipelineItem

    item = db.query(PipelineItem).one()
    with respx.mock() as mock:
        mock.post("https://api.anthropic.com/v1/messages").mock(
            return_value=Response(
                200,
                json={"content": [{"type": "text", "text": "CIRCIA NPRM comment window is open."}]},
            )
        )
        draft = summarize_item(db, item.id)
    rendered = draft.render()
    assert "AI-ASSISTED DRAFT" in rendered
    assert "not legal or compliance advice" in rendered
    assert "CIRCIA NPRM" in rendered
