"""M8 tests: feed-first + browser-rendered adapters. All fixture-driven — no network,
no browser in CI (the EBA fixture is a saved headless-Chromium DOM serialization)."""

from __future__ import annotations

from datetime import date

import pytest

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.cis import CisAdapter
from oblag.adapters.cppa import CppaAdapter
from oblag.adapters.eba import EbaAdapter
from oblag.adapters.edpb import EdpbAdapter
from oblag.adapters.esma import EsmaAdapter
from oblag.adapters.nerc import NercAdapter
from oblag.core.reducer import reduce_item
from oblag.db.models import Confidence, DateType, ItemState


def _normalize(adapter, *fixture):
    raw = RawDocument(url="https://test", content=load_fixture(*fixture))
    return list(adapter.normalize(raw))


# --- EDPB ---


def test_edpb_filters_to_formal_signals(db):
    items = _normalize(EdpbAdapter(), "edpb", "news.rss")
    # live feed: 10 news items, only the adopted-guidance one is a formal signal
    assert 1 <= len(items) <= 3
    assert all(i.obligation_slug == "gdpr" for i in items)
    adopted = [i for i in items if i.native_status == "adopted"]
    assert adopted, "expected the 'adopts' guidance item to be captured"
    res = reduce_item(db, adopted[0], today=date(2026, 7, 18))
    assert res.item.state is ItemState.effective


def test_edpb_consultation_deadline_parsing():
    adapter = EdpbAdapter()
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>EDPB launches public consultation on Guidelines 01/2026 on AI compliance</title>
    <link>https://www.edpb.europa.eu/news/x</link>
    <description>Comments accepted until 15 September 2026.</description>
    <pubDate>Wed, 08 Jul 2026 10:00:00 +0200</pubDate></item>
    </channel></rss>"""
    (item,) = adapter.normalize(RawDocument(url="https://t", content=rss))
    dates = {d.date_type: d.value for d in item.dates}
    assert dates[DateType.comment_open] == date(2026, 7, 8)
    assert dates[DateType.comment_close] == date(2026, 9, 15)
    assert item.track == "proposed"


# --- ESMA ---


def test_esma_consultation_filter():
    items = _normalize(EsmaAdapter(), "esma", "news.rss")
    # current live feed window has no 'consults' items — filter yields nothing (no noise)
    assert items == []
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>ESMA consults on DORA RTS on ICT subcontracting</title>
    <link>https://www.esma.europa.eu/press-news/consultations/x</link>
    <description>&lt;time datetime="2026-07-10T09:00:00+02:00"&gt;10 July 2026
    &lt;/time&gt;</description>
    </item></channel></rss>"""
    (item,) = EsmaAdapter().normalize(RawDocument(url="https://t", content=rss))
    assert item.obligation_slug == "dora"
    assert {d.date_type for d in item.dates} == {DateType.comment_open}
    # the feed never carries the response deadline — that must surface, not stay silent
    assert any("deadline" in a for a in item.anomalies)


# --- CPPA ---


def test_cppa_sections_and_scope(db):
    items = _normalize(CppaAdapter(), "cppa", "regulations.html")
    assert items, "expected rulemaking package items"
    by_status = {}
    for i in items:
        by_status.setdefault(i.native_status, []).append(i)
    # live page: no active proposed packages; completed packages present
    assert "completed" in by_status
    assert all(i.obligation_slug == "ccpa" for i in items)
    # "Preliminary Rulemaking Activities" (weak signals) never ingested
    assert not any("preliminary" in i.title.lower() for i in items)
    completed = by_status["completed"][0]
    res = reduce_item(db, completed, today=date(2026, 7, 18))
    assert res.item.state is ItemState.effective
    # month-precision adopted dates are marked derived, never firm
    dated = [i for i in by_status["completed"] if i.dates]
    if dated:
        assert all(d.confidence is Confidence.derived for i in dated for d in i.dates)


# --- CIS ---


def test_cis_release_filter_rejects_noise():
    # live blog fixture: community/marketing posts only → zero items (strict filter)
    assert _normalize(CisAdapter(), "cis", "blog.rss") == []
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Announcing CIS Critical Security Controls v8.2</title>
    <link>https://www.cisecurity.org/blog/x</link>
    <pubDate>Mon, 01 Jun 2026 12:00:00 +0000</pubDate></item>
    </channel></rss>"""
    (item,) = CisAdapter().normalize(RawDocument(url="https://t", content=rss))
    assert item.external_key == ("cis_release", "8.2")
    assert item.obligation_slug == "cis-controls"


# --- EBA (browser-rendered fixture) ---


def test_eba_rendered_listing_parses_consultation_windows(db):
    items = _normalize(EbaAdapter(), "eba", "consultations_rendered.html")
    assert len(items) >= 10  # 15 rows rendered live
    mica = next(i for i in items if "MiCA" in i.title and "fines" in i.title.lower())
    assert mica.external_key[0] == "eba_cp"  # EBA/CP reference is the durable key
    dates = {d.date_type: d.value for d in mica.dates}
    assert dates[DateType.comment_open] == date(2026, 6, 26)
    assert dates[DateType.comment_close] == date(2026, 9, 28)
    res = reduce_item(db, mica, today=date(2026, 7, 18))
    assert res.item.state is ItemState.comment_open
    from oblag.core.reducer import tick

    events = tick(db, today=date(2026, 9, 29))
    assert [e.payload["to"] for e in events] == ["comment_closed"]


def test_eba_year_spanning_window_rolls_open_year_back():
    # live fixture row: "5 Dec … 5 Mar 2025" — opened Dec 2024, not Dec 2025
    items = _normalize(EbaAdapter(), "eba", "consultations_rendered.html")
    spanning = [
        i
        for i in items
        for d in i.dates
        if d.date_type is DateType.comment_open and d.value.month == 12
    ]
    assert spanning, "expected the December-opening consultation from the live fixture"
    for item in spanning:
        dates = {d.date_type: d.value for d in item.dates}
        assert dates[DateType.comment_open] < dates[DateType.comment_close]
        assert dates[DateType.comment_open] == date(2024, 12, 5)
        assert dates[DateType.comment_close] == date(2025, 3, 5)


def test_eba_disabled_without_browser(monkeypatch):
    import oblag.browserfetch as bf

    monkeypatch.setattr(bf, "browser_available", lambda: False)
    assert EbaAdapter().enabled() is False


# --- NERC ---


def test_nerc_listing_selects_recent_cyber_projects_only():
    """Project selection: CIP/cyber slugs only, 2020+ only — never the webinar prose
    that fabricated 'Project 2025-03: and Project 2025-04' titles (observed live)."""
    from oblag.adapters.nerc import _cyber_projects

    listing = load_fixture("nerc", "listing.html").decode()
    numbers = [n for n, _ in _cyber_projects(listing)]
    assert numbers == ["2022-05", "2023-03", "2023-09", "2025-06"]
    # excluded: 2025-03 (Order 901 studies — not cyber), 2026-02 (computational
    # loads), 2008-06 (cyber but long-completed, below the year floor)


def test_nerc_project_page_normalize_and_state(db):
    raw = RawDocument(
        url="https://www.nerc.com/standards/reliability-standards-under-development/2022-05-modifications-to-cip-008-reporting-threshold",
        content=load_fixture("nerc", "project_page.html"),
        meta={
            "kind": "project",
            "slug": "2022-05-modifications-to-cip-008-reporting-threshold",
            "number": "2022-05",
        },
    )
    (item,) = NercAdapter().normalize(raw)
    assert item.title == "NERC Project 2022-05: Modifications to CIP-008 reporting threshold"
    assert item.native_status == "45-day formal comment period with initial ballot"
    assert item.obligation_slug == "nerc-cip"
    res = reduce_item(db, item, today=date(2026, 7, 22))
    assert res.item.state is ItemState.comment_open


def test_nerc_statemap_status_texts():
    from oblag.core.statemap import nerc_statemap

    cases = {
        "Board adopted and filed with FERC": ItemState.final_pending_effective,
        "45-day formal comment period with initial ballot": ItemState.comment_open,
        "Final ballot": ItemState.comment_closed,
        "Drafting team formation": ItemState.proposed,
    }
    for text, want in cases.items():
        assert nerc_statemap(text, {}, {}, date(2026, 7, 22)) is want, text


def test_nerc_restructured_page_is_anomaly_not_silence():
    raw = RawDocument(
        url="https://t",
        content=b"<html>nothing</html>",
        meta={"kind": "listing", "projects_found": "0"},
    )
    (sentinel,) = list(NercAdapter().normalize(raw))
    assert sentinel.anomalies


# --- browser tier ---


def test_browser_unavailable_is_clean(monkeypatch):
    import oblag.browserfetch as bf

    monkeypatch.setattr(bf, "browser_available", lambda: False)
    with pytest.raises(bf.BrowserUnavailable):
        bf.render_page("https://example.com/")
