"""AIUC-1: quarterly releases, and the one that is scheduled but hasn't landed."""

from __future__ import annotations

from datetime import date

from conftest import load_fixture
from oblag.adapters.aiuc import AiucAdapter
from oblag.adapters.base import RawDocument
from oblag.core.statemap import compute_state
from oblag.db.models import Confidence, DateType, ItemState


def _items():
    raw = RawDocument(
        url="https://www.aiuc-1.com/changelog",
        content=load_fixture("aiuc", "changelog.html"),
        content_type="text/html",
    )
    return list(AiucAdapter().normalize(raw))


def test_every_release_in_the_history_becomes_a_dated_item():
    by_key = {i.external_key[1]: i for i in _items()}
    # four superseded releases from the history table, plus the current one
    for released_on in ("2025-07-22", "2025-10-01", "2026-01-15", "2026-04-15", "2026-07-15"):
        item = by_key[released_on]
        assert item.obligation_slug == "aiuc-1"
        assert item.native_status == "release"
        assert item.published_at == date.fromisoformat(released_on)
        effective = [d for d in item.dates if d.date_type is DateType.effective]
        assert effective and effective[0].confidence is Confidence.published_firm


def test_the_announced_next_release_is_carried_before_it_lands():
    """AIUC-1 publishes its next release date a quarter ahead. That is a real dated
    thing a compliance team plans around, so it belongs on the deadlines page."""
    scheduled = next(i for i in _items() if i.native_status == "scheduled")
    assert scheduled.external_key == ("aiuc_release", "2026-10-15")
    assert scheduled.track == "proposed"
    assert scheduled.published_at is None, "it has not been published yet"
    projected = [d for d in scheduled.dates if d.date_type is DateType.projected_final]
    assert projected and projected[0].value == date(2026, 10, 15)


def test_the_scheduled_row_becomes_the_released_row():
    """Same external key both sides of the release, so the row flips in place instead
    of a second row appearing beside a stale one."""
    items = _items()
    scheduled = next(i for i in items if i.native_status == "scheduled")
    a_release = next(i for i in items if i.native_status == "release")
    assert scheduled.external_key[0] == a_release.external_key[0] == "aiuc_release"
    # and the scheduled date is never also emitted as a release
    assert sum(1 for i in items if i.external_key == scheduled.external_key) == 1


def test_only_the_current_release_claims_a_summary():
    """The page carries change notes for the current release and links out for the
    rest, so an older row must not borrow the current one's summary."""
    items = _items()
    current = next(i for i in items if i.external_key[1] == "2026-07-15")
    older = next(i for i in items if i.external_key[1] == "2026-01-15")
    assert current.abstract and "coding agent" in current.abstract
    assert older.abstract is None
    # the per-requirement change tags are counted onto the current release only
    assert int(current.native_meta["revisions"]) > 0
    assert "revisions" not in older.native_meta


def test_release_dates_quoted_in_change_notes_are_not_mistaken_for_releases():
    """Change notes name other dates in prose. Only the history table and the two
    headline sentences may create releases."""
    keys = {i.external_key[1] for i in _items()}
    assert keys == {
        "2025-07-22",
        "2025-10-01",
        "2026-01-15",
        "2026-04-15",
        "2026-07-15",
        "2026-10-15",
    }


def test_statemap():
    assert compute_state("aiuc", "release", {}, {}, date(2026, 7, 26)) is ItemState.effective
    assert compute_state("aiuc", "scheduled", {}, {}, date(2026, 7, 26)) is ItemState.proposed
    assert compute_state("aiuc", "something-new", {}, {}, date(2026, 7, 26)) is None


def test_malformed_page_yields_nothing_rather_than_raising():
    raw = RawDocument(url="https://test", content=b"<html><body>nope</body></html>")
    assert list(AiucAdapter().normalize(raw)) == []


def test_a_release_advances_the_catalog_version(db):
    from oblag.catalog import seed_obligations
    from oblag.core.reducer import reduce_item
    from oblag.db.models import Obligation
    from oblag.versionsuggest import auto_apply

    seed_obligations(db)
    ob = db.query(Obligation).filter_by(slug="aiuc-1").one()
    ob.current_version = "2026-04-15"  # pretend the catalog is a quarter behind
    db.commit()

    for item in _items():
        reduce_item(db, item, today=date(2026, 7, 26))
    db.commit()
    auto_apply(db)

    db.expire_all()
    ob = db.query(Obligation).filter_by(slug="aiuc-1").one()
    assert ob.effective_version == "2026-07-15"


def test_date_versions_compare_and_are_bounded():
    """A release named by its date is a version scheme of its own. It must order
    correctly, must not be read out of a title, and must not cross schemes."""
    from oblag.versions import is_newer, plausible_successor, version_key

    assert version_key("2026-07-15") == (2026, 7, 15)
    assert is_newer("2026-07-15", "2026-04-15")
    assert not is_newer("2026-04-15", "2026-07-15")
    # trailing components are never stripped: these are different releases
    assert version_key("2026-04-10") != version_key("2026-04-01")
    # a title is not a version
    assert version_key("AIUC-1 2026-10-15 (scheduled)") is None
    assert plausible_successor("2026-04-15", "2026-07-15")
    assert plausible_successor("2025-10-01", "2026-01-15")  # across a year boundary
    assert not plausible_successor("2026-07-15", "2029-07-15")  # a mis-read year
    # scheme mismatch is a parse gone wrong, not a release
    assert not plausible_successor("11.8", "2026-07-15")
    assert not plausible_successor("2026-07-15", "12.0")
