from __future__ import annotations

import json

from oblag.adapters.base import RawDocument
from oblag.adapters.federal_register import FederalRegisterAdapter
from oblag.scope import in_scope


def test_in_scope_vocabulary():
    assert in_scope("Cyber Incident Reporting for Critical Infrastructure Act")
    assert in_scope("Review of ENISA Regulation and EU ICT security certification")
    assert in_scope("Cloud and AI Development Act")
    assert in_scope("Guidelines on the processing of personal data")
    # off-scope
    assert not in_scope("Pacific Halibut Fisheries; Inseason Adjustment")
    assert not in_scope("Airworthiness Directives; Boeing Company Airplanes")
    assert not in_scope("Drawbridge Operation Regulation; Newark Bay")
    assert not in_scope("New Radio Spectrum Policy Programme (RSPP 2.0)")
    # 'ai' must be an exact word, never a prefix inside another word
    assert not in_scope("Air quality standards for aircraft maintenance")


def test_in_scope_extra_terms(monkeypatch):
    from oblag.config import get_settings

    monkeypatch.setenv("OBLAG_SCOPE_EXTRA_TERMS", "halibut")
    get_settings.cache_clear()
    assert in_scope("Pacific Halibut Fisheries")
    get_settings.cache_clear()


def test_in_scope_gate_disabled(monkeypatch):
    from oblag.config import get_settings

    monkeypatch.setenv("OBLAG_SCOPE_FILTER", "false")
    get_settings.cache_clear()
    assert in_scope("Pacific Halibut Fisheries")
    get_settings.cache_clear()


def _fr_doc(title, doc_type="Rule", **extra):
    doc = {
        "document_number": "2026-99999",
        "title": title,
        "type": doc_type,
        "action": "Final rule.",
        "publication_date": "2026-07-17",
        "regulation_id_numbers": [],
        "docket_ids": [],
        "agencies": [],
    }
    doc.update(extra)
    return RawDocument(url="https://t", content=json.dumps({"results": [doc]}).encode())


def test_federal_register_drops_off_scope_documents():
    adapter = FederalRegisterAdapter()
    assert list(adapter.normalize(_fr_doc("Pacific Halibut Fisheries; Inseason Action"))) == []
    kept = list(
        adapter.normalize(
            _fr_doc("Cyber Incident Reporting for Critical Infrastructure Act Requirements")
        )
    )
    assert len(kept) == 1
    # abstract alone can put a blandly-titled rule in scope
    kept2 = list(
        adapter.normalize(
            _fr_doc("Amendments to Part 160", abstract="updates data breach notification duties")
        )
    )
    assert len(kept2) == 1
