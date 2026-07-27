"""UK GDPR: the obligation that had no source of any kind."""

from __future__ import annotations

from datetime import date

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.uk_legislation import WATCHED, UkLegislationAdapter
from oblag.core.statemap import compute_state
from oblag.db.models import DateType, ItemState


# The fixture's ukm:Effect ids are rewritten to key-test-effect-NN. Upstream they are
# "key-" plus 32 hex characters, which is byte-for-byte the shape of a Mailgun API key,
# so GitHub push protection blocks the file. The adapter never reads EffectId, so the
# fixture loses nothing by carrying placeholders. Do not "restore" the real ones.
def _items():
    raw = RawDocument(
        url="https://www.legislation.gov.uk/changes/affected/eur/2016/679/data.feed",
        content=load_fixture("uk_legislation", "uk_gdpr_changes.xml"),
        content_type="application/atom+xml",
        meta={"act": "uk-gdpr"},
    )
    return list(UkLegislationAdapter().normalize(raw))


def test_one_row_per_amending_instrument_not_per_provision():
    """The Data (Use and Access) Act regulations touch dozens of articles. Fifty
    near-identical rows saying so would bury the fact a reader needs."""
    items = _items()
    assert len(items) == 2, [i.title for i in items]
    titles = {i.title for i in items}
    assert "UK GDPR amended by Children’s Wellbeing and Schools Act 2026" in titles
    assert all(i.obligation_slug == "uk-gdpr" for i in items)


def test_a_commenced_amendment_is_law_and_carries_its_date():
    item = next(i for i in _items() if "Children’s Wellbeing" in i.title)
    assert item.native_status == "in_force"
    assert item.published_at == date(2026, 4, 29)
    effective = [d for d in item.dates if d.date_type is DateType.effective]
    assert effective and effective[0].value == date(2026, 4, 29)
    assert "In force since 2026-04-29" in item.abstract


def test_a_prospective_amendment_says_so_instead_of_inventing_a_date():
    """legislation.gov.uk marks these Applied=false, Prospective=true with no Date: the
    instrument commences on a day to be appointed. That day does not exist yet, so no
    compliance date can be stated and none is."""
    item = next(i for i in _items() if "Consequential" in i.title)
    assert item.native_status == "pending"
    assert item.published_at is None
    assert item.dates == []
    assert "day to be appointed" in item.abstract


def test_the_abstract_says_what_was_touched_and_how():
    item = next(i for i in _items() if "Children’s Wellbeing" in i.title)
    assert "inserted" in item.abstract
    assert "Art. 8" in item.abstract
    assert item.native_meta["provisions"]


def test_identity_pairs_the_affected_act_with_the_affecting_one():
    """The same instrument reappears as more of its provisions commence, so it has to
    update its row rather than stack a new one beside it."""
    keys = [i.external_key for i in _items()]
    assert all(t == "uk_effect" for t, _ in keys)
    assert all(v.startswith("uk-gdpr|") for _t, v in keys)
    assert len(set(keys)) == len(keys)


def test_both_halves_of_the_obligation_are_watched():
    """The catalog tracks 'UK GDPR + Data Protection Act 2018' as one obligation, so
    both statutes have to feed it."""
    assert {a.path for a in WATCHED} == {"eur/2016/679", "ukpga/2018/12"}
    assert {a.obligation for a in WATCHED} == {"uk-gdpr"}


def test_statemap():
    today = date(2026, 7, 27)
    assert compute_state("uk_legislation", "in_force", {}, {}, today) is ItemState.effective
    assert (
        compute_state("uk_legislation", "pending", {}, {}, today)
        is ItemState.final_pending_effective
    )
    assert compute_state("uk_legislation", "other", {}, {}, today) is None


def test_a_broken_feed_yields_nothing_rather_than_raising():
    for content in (b"", b"<html>nope</html>", b"<feed></feed>"):
        raw = RawDocument(url="t", content=content, meta={"act": "uk-gdpr"})
        assert list(UkLegislationAdapter().normalize(raw)) == []


def test_an_unknown_act_is_not_normalized():
    raw = RawDocument(
        url="t",
        content=load_fixture("uk_legislation", "uk_gdpr_changes.xml"),
        meta={"act": "not-watched"},
    )
    assert list(UkLegislationAdapter().normalize(raw)) == []
