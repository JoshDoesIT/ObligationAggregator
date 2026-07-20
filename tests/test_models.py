from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from oblag.db.models import (
    Confidence,
    DateType,
    ItemState,
    JoinKey,
    KeyDate,
    Obligation,
    PipelineItem,
    PrivateDocument,
)


def make_item(**kw) -> PipelineItem:
    defaults = dict(
        source_system="federal_register",
        jurisdiction="US-Federal",
        title="Test NPRM",
        state=ItemState.comment_open,
    )
    defaults.update(kw)
    return PipelineItem(**defaults)


def test_join_key_shared_across_tracks_but_unique_per_item(db):
    # the same RIN on two items (proposed + final track) is legal — the linker relies on it
    a, b = make_item(track="proposed"), make_item(title="final rule", track="final")
    db.add_all([a, b])
    db.flush()
    db.add(JoinKey(pipeline_item_id=a.id, type="rin", value="1670-AA04"))
    db.add(JoinKey(pipeline_item_id=b.id, type="rin", value="1670-AA04"))
    db.flush()
    # but duplicated on the same item is not
    db.add(JoinKey(pipeline_item_id=a.id, type="rin", value="1670-AA04"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_key_date_supersession_chain(db):
    item = make_item()
    db.add(item)
    db.flush()
    d1 = KeyDate(
        pipeline_item_id=item.id,
        date_type=DateType.comment_close,
        value=date(2024, 6, 3),
        confidence=Confidence.published_firm,
    )
    db.add(d1)
    db.flush()
    d2 = KeyDate(
        pipeline_item_id=item.id,
        date_type=DateType.comment_close,
        value=date(2024, 7, 3),
        confidence=Confidence.published_firm,
        supersedes_id=d1.id,
    )
    db.add(d2)
    db.flush()
    # both rows persist (append-only); the chain is queryable
    rows = db.query(KeyDate).filter_by(pipeline_item_id=item.id).all()
    assert len(rows) == 2
    assert d2.supersedes_id == d1.id


def test_private_document_version_unique_per_obligation(db):
    ob = Obligation(slug="pci-dss", name="PCI DSS", issuing_body="PCI SSC", jurisdiction="Global")
    db.add(ob)
    db.flush()
    db.add(
        PrivateDocument(
            obligation_id=ob.id, version_label="4.0.1", sha256="ab" * 32, storage_ref="x"
        )
    )
    db.flush()
    db.add(
        PrivateDocument(
            obligation_id=ob.id, version_label="4.0.1", sha256="cd" * 32, storage_ref="y"
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_obligation_defaults_are_conservative_enough(db):
    ob = Obligation(slug="x", name="X", issuing_body="Y", jurisdiction="Z")
    db.add(ob)
    db.flush()
    # defaults exist; copyrighted obligations must be set explicitly by seed data
    assert ob.display_policy.value == "full_text"
    assert ob.copyright_status.value == "public_domain"


def test_init_db_backfills_retracted_column(tmp_path):
    """Databases created before v0.1.7 lack key_date.retracted; init_db adds it."""
    from sqlalchemy import create_engine, inspect, text

    from oblag.db.models import Base
    from oblag.db.session import init_db

    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE key_date DROP COLUMN retracted"))
    assert "retracted" not in {c["name"] for c in inspect(eng).get_columns("key_date")}
    init_db(eng)
    assert "retracted" in {c["name"] for c in inspect(eng).get_columns("key_date")}
