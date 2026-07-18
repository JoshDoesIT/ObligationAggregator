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


def test_eba_disabled_without_browser(monkeypatch):
    import oblag.browserfetch as bf

    monkeypatch.setattr(bf, "browser_available", lambda: False)
    assert EbaAdapter().enabled() is False


# --- NERC ---


def test_nerc_projects_extracted(db):
    items = _normalize(NercAdapter(), "nerc", "under_development.html")
    numbers = {i.external_key[1] for i in items}
    assert {"2025-03", "2025-04"} <= numbers
    assert all(i.obligation_slug == "nerc-cip" for i in items)
    res = reduce_item(db, items[0], today=date(2026, 7, 18))
    assert res.item.state is ItemState.proposed


def test_nerc_restructured_page_is_anomaly_not_silence():
    (sentinel,) = list(
        NercAdapter().normalize(RawDocument(url="https://t", content=b"<html>nothing</html>"))
    )
    assert sentinel.anomalies


# --- browser tier ---


def test_browser_unavailable_is_clean(monkeypatch):
    import oblag.browserfetch as bf

    monkeypatch.setattr(bf, "browser_available", lambda: False)
    with pytest.raises(bf.BrowserUnavailable):
        bf.render_page("https://example.com/")
