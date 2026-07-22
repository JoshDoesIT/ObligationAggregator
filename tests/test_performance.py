from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import event

from oblag.adapters.base import NormalizedDate, NormalizedItem
from oblag.catalog import seed_obligations
from oblag.core.reducer import current_dates, current_dates_bulk, reduce_item
from oblag.db.models import Confidence, DateType, PipelineItem


def _seed_items(db, n):
    seed_obligations(db)
    for i in range(n):
        reduce_item(
            db,
            NormalizedItem(
                source_system="pci_ssc",
                external_key=("pci_doc", f"rfc-{i}"),
                jurisdiction="Global",
                title=f"PCI SSC RFC: item {i}",
                native_status="rfc",
                track="proposed",
                obligation_slug="pci-dss",
                dates=[
                    NormalizedDate(
                        DateType.comment_close,
                        date.today() + timedelta(days=i + 1),
                        Confidence.published_firm,
                    )
                ],
            ),
        )
    db.commit()


def test_current_dates_bulk_matches_per_item(db):
    _seed_items(db, 5)
    ids = [i for (i,) in db.query(PipelineItem.id)]
    bulk = current_dates_bulk(db, ids)
    for iid in ids:
        assert {k: v.id for k, v in bulk.get(iid, {}).items()} == {
            k: v.id for k, v in current_dates(db, iid).items()
        }
    assert current_dates_bulk(db, []) == {}


def test_item_list_query_count_is_bounded(client, db):
    """The feed must not scale queries with item count (was ~3×N: a dates query plus
    join-key/obligation lazy loads per item). Bulk dates + eager loads cap it."""
    _seed_items(db, 25)
    engine = db.get_bind()
    counts = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        counts["n"] += 1

    try:
        r = client.get("/api/v1/items?limit=25")
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert r.status_code == 200
    assert len(r.json()["items"]) == 25
    # a handful of queries regardless of N — not one-per-item
    assert counts["n"] <= 12, f"{counts['n']} queries for 25 items — N+1 regression"


def test_cdn_cache_header_when_auth_disabled(client, seeded):
    # single-org (auth off): global read pages are CDN-cacheable
    assert "s-maxage" in client.get("/").headers.get("cache-control", "")
    assert "s-maxage" in client.get("/api/v1/items").headers.get("cache-control", "")
    # internal/admin never cached
    assert "s-maxage" not in client.get("/admin/versions").headers.get("cache-control", "")


def test_scoped_version_pass_only_touches_given_obligations(db):
    from oblag import versionsuggest
    from oblag.db.models import Obligation

    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-pts-hsm").update({Obligation.current_version: "4.0"})
    db.commit()
    reduce_item(
        db,
        NormalizedItem(
            source_system="pci_ssc",
            external_key=("pci_pub", "hsm-5"),
            jurisdiction="Global",
            title="PCI SSC Publishes PCI PTS HSM v5.0",
            native_status="publication",
            track="final",
            obligation_slug="pci-pts-hsm",
            native_meta={"published_version": "5.0"},
        ),
    )
    db.commit()
    hsm_id = db.query(Obligation.id).filter_by(slug="pci-pts-hsm").scalar()
    other_id = db.query(Obligation.id).filter_by(slug="pci-dss").scalar()

    # scoped to an unrelated obligation → no action
    assert versionsuggest.auto_apply(db, only_ids={other_id}) == []
    # scoped to the touched obligation → applied
    assert versionsuggest.auto_apply(db, only_ids={hsm_id})[0]["applied"] is True
