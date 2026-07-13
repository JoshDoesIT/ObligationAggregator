from __future__ import annotations

from datetime import date

from oblag.adapters.base import NormalizedDate, NormalizedItem
from oblag.core.linker import link_resolved_items
from oblag.core.reducer import reduce_item
from oblag.db.models import Confidence, DateType, EventType, ItemState

TODAY = date(2026, 6, 1)


def _item(doc: str, track: str, native: str, dates: list[NormalizedDate], **kw):
    defaults = dict(
        source_system="federal_register",
        external_key=("fr_doc_number", doc),
        jurisdiction="US-Federal",
        title=f"Doc {doc}",
        native_status=native,
        track=track,
        dates=dates,
    )
    defaults.update(kw)
    return NormalizedItem(**defaults)


def test_rin_links_proposed_to_final(db):
    proposed = reduce_item(
        db,
        _item(
            "2024-06526",
            "proposed",
            "PRORULE",
            [NormalizedDate(DateType.comment_close, date(2024, 7, 3), Confidence.published_firm)],
            join_keys=[("rin", "1670-AA04")],
        ),
        today=TODAY,
    ).item
    final = reduce_item(
        db,
        _item(
            "2026-10000",
            "final",
            "RULE",
            [NormalizedDate(DateType.effective, date(2026, 10, 1), Confidence.published_firm)],
            join_keys=[("rin", "1670-AA04")],
        ),
        today=TODAY,
    ).item

    events = link_resolved_items(db)
    assert {e.type for e in events} == {EventType.item_resolved, EventType.state_changed}
    db.refresh(proposed)
    assert proposed.resolved_change_id == final.id
    assert proposed.state is ItemState.superseded
    assert final.state is ItemState.final_pending_effective
    # idempotent
    assert link_resolved_items(db) == []


def test_docket_alone_does_not_link(db):
    # docket ids are shared by many documents; only strong keys (RIN…) assert lineage
    reduce_item(
        db,
        _item("2024-1", "proposed", "PRORULE", [], join_keys=[("docket_id", "CISA-2022-0010")]),
        today=TODAY,
    )
    reduce_item(
        db,
        _item("2026-2", "final", "RULE", [], join_keys=[("docket_id", "CISA-2022-0010")]),
        today=TODAY,
    )
    assert link_resolved_items(db) == []


def test_withdrawn_proposed_is_not_linked(db):
    reduce_item(
        db,
        _item(
            "2024-3",
            "proposed",
            "PRORULE",
            [],
            join_keys=[("rin", "0000-AA00")],
            native_meta={"action": "Proposed rule; withdrawal"},
        ),
        today=TODAY,
    )
    reduce_item(
        db,
        _item("2026-4", "final", "RULE", [], join_keys=[("rin", "0000-AA00")]),
        today=TODAY,
    )
    assert link_resolved_items(db) == []
