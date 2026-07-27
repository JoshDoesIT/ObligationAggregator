"""An adapter that changes HOW it tracks a source must not duplicate that source."""

from __future__ import annotations

from datetime import date

from oblag.adapters.base import NormalizedItem
from oblag.core.reducer import reduce_item
from oblag.db.models import PipelineItem


def _iso(track: str, native: str, title: str) -> NormalizedItem:
    return NormalizedItem(
        source_system="iso_catalog",
        external_key=("iso_project", "https://www.iso.org/standard/27018"),
        jurisdiction="Global",
        title=title,
        native_status=native,
        track=track,
    )


def test_a_track_change_updates_the_row_instead_of_doubling_it(db):
    """iso_catalog moved from track 'default' to 'final' in v0.19.0. The reducer's
    same-track guard refused to match across that, so every base ISO standard came
    back a second time beside its old row (observed live, 6 duplicates)."""
    first = reduce_item(db, _iso("default", "60.60", "ISO/IEC 27018:2025"), today=date(2026, 7, 27))
    db.commit()
    second = reduce_item(
        db, _iso("final", "published", "ISO/IEC 27018:2025"), today=date(2026, 7, 27)
    )
    db.commit()

    assert not second.created
    assert second.item.id == first.item.id
    assert db.query(PipelineItem).filter_by(source_system="iso_catalog").count() == 1
    assert second.item.track == "final"


def test_a_shared_umbrella_key_still_never_merges_two_documents(db):
    """The exact-key shortcut must not reopen the umbrella-key hole: two FR documents
    sharing an agency-wide RIN are distinct rulemakings."""
    common = [("rin", "2120-AA64")]
    a = NormalizedItem(
        source_system="federal_register",
        external_key=("fr_doc_number", "2026-0001"),
        jurisdiction="US-Federal",
        title="Airworthiness Directive A",
        native_status="RULE",
        track="final",
        join_keys=common,
    )
    b = NormalizedItem(
        source_system="federal_register",
        external_key=("fr_doc_number", "2026-0002"),
        jurisdiction="US-Federal",
        title="Airworthiness Directive B",
        native_status="RULE",
        track="final",
        join_keys=common,
    )
    reduce_item(db, a, today=date(2026, 7, 27))
    reduce_item(db, b, today=date(2026, 7, 27))
    db.commit()
    assert db.query(PipelineItem).filter_by(source_system="federal_register").count() == 2


def test_the_repair_retires_rows_already_split(db):
    """Rows split before the reducer fix still need clearing. The survivor is the one
    the current adapter is maintaining, i.e. the most recently seen."""
    from oblag.maintenance import dedupe_split_identities

    old = reduce_item(db, _iso("default", "60.60", "ISO/IEC 27018:2025"), today=date(2026, 7, 27))
    db.commit()
    old_id = old.item.id
    # simulate the pre-fix split: a second row under the same external key
    new = PipelineItem(
        source_system="iso_catalog",
        jurisdiction="Global",
        title="ISO/IEC 27018:2025",
        state=old.item.state,
        native_status="published",
        track="final",
        content_fingerprint="x",
    )
    db.add(new)
    db.flush()
    from oblag.db.models import JoinKey

    db.add(
        JoinKey(
            pipeline_item_id=new.id,
            type="iso_project",
            value="https://www.iso.org/standard/27018",
        )
    )
    db.commit()
    assert db.query(PipelineItem).filter_by(source_system="iso_catalog").count() == 2

    result = dedupe_split_identities(db)
    db.commit()
    assert result["purged"] == [old_id]
    assert db.query(PipelineItem).filter_by(source_system="iso_catalog").count() == 1
    assert db.query(PipelineItem).one().track == "final"


def test_the_repair_is_a_no_op_on_clean_data(db):
    from oblag.maintenance import dedupe_split_identities

    reduce_item(db, _iso("final", "published", "ISO/IEC 27018:2025"), today=date(2026, 7, 27))
    db.commit()
    assert dedupe_split_identities(db) == {"purged": [], "kept": []}
