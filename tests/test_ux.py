"""UX structure guarantees: the information architecture decisions are load-bearing
(a nav that regrows to nine tabs, or a filter form that goes back to asking for typed
enum CSV, is a regression), so they're asserted rather than left to review."""

from __future__ import annotations

import re


def _nav(html: str) -> str:
    m = re.search(r"<nav class=\"site\">(.*?)</nav>", html, re.S)
    assert m, "site nav not found"
    return m.group(1)


def test_primary_nav_is_daily_use_only_with_utility_menu(client, seeded):
    """Only daily-use destinations at top level; operator/reference surfaces stay
    reachable but demoted into the More menu."""
    nav = _nav(client.get("/changes").text)
    primary = re.findall(r"<a href=\"([^\"]+)\" class=\"\{?[^\"]*\"[^>]*>([^<]+)</a>", nav)
    top = [label.strip() for href, label in primary if 'class="' not in href]
    # the primary labels appear outside the utility menu
    menu = nav.split('<div class="utilmenu">')[1] if "utilmenu" in nav else ""
    head = nav.split('<div class="utilmenu">')[0]
    for label in ("Changes", "Obligations", "Deadlines", "Watchlists"):
        assert label in head, f"{label} should be a primary nav item"
    assert "Documents" not in nav, "the BYOL tab was removed in v0.9.0"
    assert top  # sanity: the regex found anchors
    # operator/reference surfaces are present but inside the menu, not at top level
    for label in ("Activity", "Sources", "API"):
        assert label in menu, f"{label} belongs in the More menu"
        assert label not in head, f"{label} should not be a primary nav item"


def test_feed_page_leads_with_needs_attention(client, seeded):
    html = client.get("/changes").text
    assert 'aria-label="Needs attention"' in html
    for label in ("comment windows closing", "deadlines in 30 days", "awaiting outcome"):
        assert label in html
    # the attention band replaced the passive count tiles
    assert 'class="stats"' not in html


def test_feed_rows_are_three_columns(client, seeded):
    """Kind and Source no longer own columns — they ride a muted subtitle so the row
    scans Change → State → Key dates."""
    html = client.get("/changes").text
    header = re.search(r"<table class=\"rows feed[^\"]*\">.*?<thead>(.*?)</thead>", html, re.S)
    assert header
    cols = re.findall(r"<th>([^<]*)</th>", header.group(1))
    assert cols == ["Change", "State", "Key dates"]
    assert '<div class="sub">' in html  # the facets moved into the subtitle line


def test_item_detail_collapses_audit_surfaces(client, seeded, db):
    from oblag.db.models import PipelineItem

    item = db.query(PipelineItem).filter_by(source_system="federal_register").first()
    html = client.get(f"/items/{item.id}").text
    # the audit trail is present but behind one disclosure, not four stacked panels
    assert "History &amp; provenance" in html
    body = html.split("History &amp; provenance")[1]
    for heading in ("Date history", "Activity", "Identifiers", "Provenance"):
        assert heading in body or heading not in html, f"{heading} should sit inside the disclosure"
    # subscription language is reserved for subscriptions
    assert "Subscribe" in html


def test_watchlist_filters_are_checkboxes_not_typed_csv(client, seeded):
    html = client.get("/watchlists").text
    for field in ("states", "event_types", "source_systems", "obligation_slugs"):
        assert f'type="checkbox" name="{field}"' in html, f"{field} should be pickable"
    # no free-text CSV inputs for pipeline vocabulary
    assert 'name="states" placeholder' not in html
    assert "More filters" in html


def test_watchlist_creation_accepts_repeated_checkbox_values(client, seeded, db):
    """The form posts repeated fields; filters must land as lists, not one CSV string."""
    from oblag.db.models import Watchlist

    r = client.post(
        "/watchlists",
        data={
            "name": "EU cyber",
            "channel": "rss",
            "obligation_slugs": ["gdpr", "dora"],  # repeated fields, as checkboxes post
            "states": ["comment_open", "effective"],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    wl = db.query(Watchlist).filter_by(name="EU cyber").one()
    assert sorted(wl.filters["obligation_slugs"]) == ["dora", "gdpr"]
    assert sorted(wl.filters["states"]) == ["comment_open", "effective"]


def test_watchlist_creation_still_accepts_legacy_csv(client, seeded, db):
    """API/older clients may still send one comma-separated value per field."""
    from oblag.db.models import Watchlist

    r = client.post(
        "/watchlists",
        data={"name": "legacy", "channel": "rss", "states": "comment_open, effective"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    wl = db.query(Watchlist).filter_by(name="legacy").one()
    assert sorted(wl.filters["states"]) == ["comment_open", "effective"]


def test_confidence_renders_two_tiers(client, seeded):
    """Four confidence levels collapse to firm/estimated visually; the precise level
    stays in the tooltip."""
    html = client.get("/deadlines").text
    assert ("conf-firm" in html) or ("conf-est" in html)
    assert "conf-statutory_hard" not in html  # the four-way colour encoding is gone


def test_all_pages_render(client, seeded):
    for path in (
        "/",
        "/changes",
        "/obligations",
        "/deadlines",
        "/watchlists",
        "/events",
        "/health",
    ):
        assert client.get(path).status_code == 200, path


def test_byol_routes_are_gone(client, seeded):
    """The bring-your-own-license upload/diff surface was removed in v0.9.0: its
    identifier diff could not be made dependable across obligation families (a real
    NIST SP 800-171 r2→r3 comparison reported 136 added / 137 removed for a revision
    that actually carried 128 controls forward, purely because of renumbering)."""
    for path in ("/byol", "/byol/upload", "/byol/diff"):
        assert client.get(path).status_code == 404, path


def test_every_data_table_is_stackable_on_mobile(client, seeded, db):
    """Below 720px data tables render as labelled cards instead of real tables. Measured
    on a 375px viewport before this change: the feed's fixed columns made the state badge
    overprint the key dates and cut off the rightmost column, and the page scrolled
    horizontally (scrollWidth 427 > 375). The CSS does the work; the invariant the markup
    must hold is that NO data table ships without the `stack` class."""
    from oblag.db.models import PipelineItem

    item = db.query(PipelineItem).filter_by(source_system="federal_register").first()
    for path in (
        "/",
        "/deadlines",
        "/obligations",
        "/watchlists",
        "/events",
        "/health",
        f"/items/{item.id}",
    ):
        html = client.get(path).text
        tables = re.findall(r"<table([^>]*)>", html)
        for attrs in tables:
            assert "stack" in attrs, f"{path} has a table without mobile stacking: <table{attrs}>"
        # labelled cells only exist once there are rows — an empty table renders a
        # single colspan placeholder instead
        if tables and 'class="empty"' not in html:
            assert "data-label=" in html, f"{path} cells need mobile labels"


def test_mobile_viewport_and_touch_rules_present(client, seeded):
    """The responsive rules are inline in base.html — assert the load-bearing ones so a
    future edit can't silently drop mobile support."""
    html = client.get("/changes").text
    assert 'name="viewport"' in html and "width=device-width" in html
    assert "@media (max-width:720px)" in html
    assert "@media (pointer:coarse)" in html  # 44px touch targets
    assert "table.stack" in html
    # the More menu must escape the horizontally-scrolling nav on mobile
    assert "position:fixed" in html


def test_homepage_is_a_live_landing_page(client, seeded):
    """/ is the front door: a hero radar drawn from real deadline data and live stat
    links into the app. Not a mock — the dots must be real item links."""
    html = client.get("/").text
    assert "Regulation moves." in html
    # front-page furniture: nameplate, dateline, and the latest-filings briefs
    assert 'class="np-title"' in html and "Gazette" in html
    assert 'class="dateline"' in html
    assert "The latest filings" in html
    assert 'aria-label="Live regulatory horizon"' in html
    # a printed page is still: no load animations at all. The only keyframe left is
    # the working alarm ring on inside-7-day deadlines.
    assert html.count("@keyframes") == 1 and "@keyframes halo" in html
    assert "prefers-reduced-motion" in html
    # the Fig. 1 dot plot draws real item links with readable labels (seeded data has
    # future deadlines)
    assert re.search(r'class="fx-row[^"]*" href="/items/\d+"', html), (
        "chart should plot live deadlines"
    )
    assert 'class="fx-axis"' in html and "TODAY" in html
    assert "tracked changes" in html and "obligations watched" in html


def test_feed_moved_to_changes_with_legacy_redirects(client, seeded):
    """Old bookmarks (/?obligation=…) must keep working: bare / is the homepage, but any
    feed query param 301s to /changes with the query intact."""
    assert "Change feed" in client.get("/changes").text
    r = client.get("/?obligation=circia", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/changes?obligation=circia"
    # and the nav's Changes entry points at the feed, not the homepage
    assert 'href="/changes"' in client.get("/").text


def test_latest_filings_orders_by_activity_not_id(client, seeded, db):
    """Legacy rows with no publication date fall back to most recent event, so an old
    item with fresh activity must outrank a newer-id item that has been quiet. Curated
    timeline entries never count as filings."""
    from datetime import timedelta

    from oblag.db.models import Event, EventType, PipelineItem, utcnow

    items = (
        db.query(PipelineItem)
        .filter(PipelineItem.source_system != "curated")  # timelines aren't filings
        .order_by(PipelineItem.id)
        .all()
    )
    oldest, newest = items[0], items[-1]
    # age every event, then give the OLDEST item the freshest activity
    db.query(Event).update({Event.occurred_at: utcnow() - timedelta(days=30)})
    db.add(
        Event(
            pipeline_item_id=oldest.id,
            type=EventType.date_changed,
            payload={},
            occurred_at=utcnow(),
        )
    )
    db.commit()

    html = client.get("/").text
    briefs = html.split("The latest filings")[1].split("In this edition")[0]
    first_link = re.search(r'href="/items/(\d+)"', briefs)
    assert first_link and int(first_link.group(1)) == oldest.id, (
        "the item with the newest activity should lead the briefs"
    )
    assert f"/items/{newest.id}" in briefs or newest.id != oldest.id  # sanity


def test_latest_filings_ranks_by_source_publication_date(client, seeded, db):
    """The column is the SOURCE's chronology. A document published two days ago must
    outrank one published months ago, even when the old one was ingested last (higher
    id, fresher events) — a backfill batch must not read as today's news (observed
    live: CSF v11.4.0 leading over v11.7.0 because both arrived in one batch)."""
    from datetime import timedelta

    from oblag.adapters.base import NormalizedItem
    from oblag.core.reducer import reduce_item
    from oblag.db.models import utcnow

    def filing(key: str, published_days_ago: int) -> int:
        res = reduce_item(
            db,
            NormalizedItem(
                source_system="hitrust",
                external_key=("hitrust_release", key),
                jurisdiction="Global",
                title=f"HITRUST CSF v{key}",
                native_status="release",
                track="final",
                published_at=(utcnow() - timedelta(days=published_days_ago)).date(),
            ),
        )
        return res.item.id

    recent_id = filing("90.1", published_days_ago=2)
    backfilled_id = filing("90.0", published_days_ago=200)  # ingested last, published long ago
    db.commit()

    html = client.get("/").text
    briefs = html.split("The latest filings")[1].split("In this edition")[0]
    links = [int(m) for m in re.findall(r'href="/items/(\d+)"', briefs)]
    assert recent_id in links, "a recently published filing belongs in the briefs"
    assert backfilled_id not in links or links.index(recent_id) < links.index(backfilled_id), (
        "publication date must beat ingestion order"
    )
    # the kicker prints the source's date, so the ordering is checkable at a glance
    assert re.search(r'class="b-kicker">\d{1,2} \w{3} \d{4} ·', briefs)


def test_abstract_renders_as_clean_paragraphs(client, seeded, db):
    """Source abstracts arrive with literal HTML entities and hard newlines (observed
    live: NIST '&nbsp;'/'&mdash;' shown verbatim, a section-by-section change list
    collapsed into one wall). They must render as clean paragraphs, and long
    abstracts fold behind a disclosure so status stays above the fold."""
    from oblag.db.models import PipelineItem

    item = db.query(PipelineItem).filter_by(source_system="federal_register").first()
    item.abstract = (
        "Storage has evolved.&nbsp;Important updates include:\n\n"
        "Section 2 &mdash; Removal of obsolete content.\n"
        "Section 3 &mdash; Completely revised threat model. " + "More detail. " * 60
    )
    db.commit()

    html = client.get(f"/items/{item.id}").text
    assert "&amp;nbsp;" not in html and "&amp;mdash;" not in html, (
        "entities must be decoded, not shown verbatim"
    )
    assert "Section 2 — Removal" in html
    body = html.split('class="lede prose"')[1].split("</div>")[0]
    assert body.count("<p>") >= 3, "hard newlines should become paragraphs"
    assert "Read the rest from the source" in html, "long abstracts fold"
