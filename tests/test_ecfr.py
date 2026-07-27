"""Obligations that are a piece of the CFR and nothing else."""

from __future__ import annotations

import json
from datetime import date

from oblag.adapters.base import RawDocument
from oblag.adapters.ecfr import WATCHED, EcfrAdapter
from oblag.core.statemap import compute_state
from oblag.db.models import DateType, ItemState


def _raw(rule_key: str, versions: list[dict]) -> RawDocument:
    return RawDocument(
        url="https://www.ecfr.gov/api/versioner/v1/versions/title-16.json?part=314",
        content=json.dumps({"content_versions": versions}).encode(),
        content_type="application/json",
        meta={"rule": rule_key},
    )


def _v(identifier: str, amended: str, part: str = "314") -> dict:
    return {
        "identifier": identifier,
        "amendment_date": amended,
        "date": amended,
        "part": part,
        "name": f"§ {identifier} Something.",
        "substantive": True,
        "removed": False,
        "type": "section",
    }


def test_the_latest_amendment_across_a_part_is_the_state_of_the_rule():
    items = list(
        EcfrAdapter().normalize(
            _raw("16-314", [_v("314.1", "2021-12-09"), _v("314.4", "2024-05-13")])
        )
    )
    assert len(items) == 1
    item = items[0]
    assert item.obligation_slug == "glba-safeguards"
    assert item.published_at == date(2024, 5, 13)
    assert "last amended 2024-05-13" in item.title
    assert item.native_meta["sections_amended"] == "314.4"
    effective = [d for d in item.dates if d.date_type is DateType.effective]
    assert effective and effective[0].value == date(2024, 5, 13)


def test_every_section_amended_on_the_latest_date_is_listed():
    items = list(
        EcfrAdapter().normalize(
            _raw(
                "16-314",
                [_v("314.2", "2024-05-13"), _v("314.5", "2024-05-13"), _v("314.1", "2021-12-09")],
            )
        )
    )
    assert items[0].native_meta["sections_amended"] == "314.2, 314.5"


def test_a_watched_section_ignores_the_rest_of_its_part():
    """Regulation S-K is 200+ sections about executive pay and mine safety. Watching the
    whole part would report every unrelated SEC amendment as a cybersecurity change."""
    versions = [
        _v("229.106", "2023-09-05", part="229"),
        _v("229.402", "2026-07-01", part="229"),  # executive compensation, much newer
    ]
    item = next(iter(EcfrAdapter().normalize(_raw("17-229-106", versions))))
    assert item.obligation_slug == "sec-cyber-disclosure"
    assert item.published_at == date(2023, 9, 5)
    assert item.native_meta["sections_amended"] == "229.106"


def test_a_part_with_no_sections_named_watches_the_whole_part():
    versions = [_v("314.1", "2021-12-09"), {"identifier": "301.1", "amendment_date": "2026-01-01"}]
    item = next(iter(EcfrAdapter().normalize(_raw("16-314", versions))))
    assert item.published_at == date(2021, 12, 9)  # the stray row is not in part 314


def test_the_citation_names_the_section_when_only_one_is_watched():
    by_key = {r.key: r for r in WATCHED}
    assert by_key["17-229-106"].citation == "17 CFR 229.106"
    assert by_key["16-314"].citation == "16 CFR 314"
    assert by_key["17-240-icfr"].citation == "17 CFR 240"


def test_the_target_is_the_identity_so_a_new_amendment_updates_the_row():
    a, b = (
        next(iter(EcfrAdapter().normalize(_raw("16-314", [_v("314.4", when)]))))
        for when in ("2021-12-09", "2024-05-13")
    )
    assert a.external_key == b.external_key == ("cfr_target", "16-314")
    assert a.title != b.title  # ...and the change is visible in it


def test_every_watched_target_names_an_obligation_and_a_reachable_page():
    for rule in WATCHED:
        assert rule.obligation
        assert rule.url.startswith("https://www.ecfr.gov/current/title-")
        assert rule.label and "(" not in rule.label  # the title wraps it in parens


def test_statemap():
    today = date(2026, 7, 27)
    assert compute_state("ecfr", "in_force", {}, {}, today) is ItemState.effective
    assert compute_state("ecfr", "something-else", {}, {}, today) is None


def test_a_shape_we_do_not_recognise_yields_nothing_rather_than_a_guess():
    for content in (
        b"",
        b"not json",
        b"{}",
        b'{"content_versions": []}',
        b'{"content_versions": 3}',
    ):
        raw = RawDocument(
            url="t", content=content, content_type="application/json", meta={"rule": "16-314"}
        )
        assert list(EcfrAdapter().normalize(raw)) == []


def test_undated_rows_never_become_the_answer():
    versions = [_v("314.1", "2021-12-09"), {"identifier": "314.4", "part": "314"}]
    item = next(iter(EcfrAdapter().normalize(_raw("16-314", versions))))
    assert item.published_at == date(2021, 12, 9)
