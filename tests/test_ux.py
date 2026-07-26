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
    assert "The rules keep moving." in html
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
    briefs = html.split("The latest filings")[1].split("How Gazette works")[0]
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
    briefs = html.split("The latest filings")[1].split("How Gazette works")[0]
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


def test_paper_theme_is_the_only_edition(client, seeded):
    """The paper prints the same in any light: no dark scheme, the browser told not to
    invent one, mobile chrome tinted to the stock, and the print grain present. A
    reintroduced dark block would silently split the design again."""
    html = client.get("/").text
    assert "prefers-color-scheme: dark" not in html
    assert "color-scheme: only light" in html
    # chrome is tinted to the paper design (the exact shade, and why, is asserted in
    # test_paper_meets_the_browser_chrome_at_the_top)
    assert 'name="theme-color"' in html and "#161310" not in html
    assert "feTurbulence" in html  # the grain is a texture, not an effect — still static
    assert html.count("@keyframes") == 1  # the stillness doctrine holds


def test_social_cards_consistent_across_platforms(client, seeded, db):
    """A shared link must render the same card everywhere: Open Graph (Facebook,
    LinkedIn, Slack, Discord, WhatsApp, Telegram, iMessage) and X's twitter: names
    mirror the same title, description and banner. The banner route serves the real
    1200x630 image."""
    html = client.get("/").text
    for tag in (
        'property="og:site_name" content="Gazette"',
        'property="og:image:width" content="1200"',
        'property="og:image:height" content="630"',
        'name="twitter:card" content="summary_large_image"',
    ):
        assert tag in html, tag
    og_title = re.search(r'property="og:title" content="([^"]*)"', html).group(1)
    tw_title = re.search(r'name="twitter:title" content="([^"]*)"', html).group(1)
    assert og_title == tw_title
    og_img = re.search(r'property="og:image" content="([^"]*)"', html).group(1)
    tw_img = re.search(r'name="twitter:image" content="([^"]*)"', html).group(1)
    assert og_img == tw_img and og_img.startswith("http") and og_img.endswith("/og-banner.jpg")

    # item pages carry their own headline + abstract into the card
    from oblag.db.models import PipelineItem

    item = db.query(PipelineItem).filter_by(source_system="federal_register").first()
    ihtml = client.get(f"/items/{item.id}").text
    assert f'property="og:title" content="{item.title} · Gazette"' in ihtml

    r = client.get("/og-banner.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert len(r.content) > 10_000  # the real banner, not a placeholder


def test_head_requests_answered_like_get(client, seeded):
    """Link crawlers and validators probe with HEAD before fetching a card; FastAPI's
    GET-only routes 405'd them app-wide. HEAD must mirror GET's status and headers
    with no body."""
    for path in ("/", "/changes", "/og-banner.jpg"):
        r = client.head(path)
        assert r.status_code == 200, path
        assert r.content == b"", path
    assert client.head("/og-banner.jpg").headers["content-type"] == "image/jpeg"


def test_paper_meets_the_browser_chrome_at_the_top(client, seeded):
    """The chrome strip sits directly above the sticky header, so theme-color must be
    the HEADER's cream, not the paper — with the paper tone you saw a band of the wrong
    shade above the menu as soon as Safari collapsed its URL bar. The header also
    carries its colour upward so elastic overscroll can't reveal a seam."""
    html = client.get("/").text
    assert 'name="theme-color" content="#faf7ec"' in html
    assert "--panel:#faf7ec" in html, "theme-color must track the header's panel colour"
    assert "header.site::before" in html and "bottom:100%" in html


def test_dateline_shows_the_readers_current_date(client, seeded):
    """The server renders today's date so the page works without JS, and a script
    rewrites it to the reader's own date: the server runs UTC, so an evening reader in
    the Americas would otherwise be shown tomorrow's date."""
    from datetime import date

    html = client.get("/").text
    server_today = date.today().strftime("%A, %d %B %Y").replace(" 0", " ")
    assert f"<span data-dateline>{server_today}</span>" in html
    # the correction exists and targets the same element
    assert "[data-dateline]" in html
    assert "DL_MONTHS" in html and "getFullYear()" in html


def test_undated_items_never_outrank_dated_ones(client, seeded, db):
    """Some sources state no publication date at all (NERC standards projects). Falling
    back to our own clock made every one of them read as today's news and fill the
    briefs column after a rebuild — the exact lie that column exists to avoid. Dated
    filings lead; undated ones appear only when there aren't enough dated ones."""
    from datetime import timedelta

    from oblag.adapters.base import NormalizedItem
    from oblag.core.reducer import reduce_item
    from oblag.db.models import utcnow

    for n in range(5):
        reduce_item(
            db,
            NormalizedItem(
                source_system="nerc",
                external_key=("nerc_project", f"2025-0{n}"),
                jurisdiction="US-Federal",
                title=f"NERC Project 2025-0{n}: undated, ingested just now",
                native_status="in_development",
                track="proposed",
            ),
        )
    dated = reduce_item(
        db,
        NormalizedItem(
            source_system="edpb",
            external_key=("edpb_item", "dated-one"),
            jurisdiction="EU",
            title="A filing that states when it was published",
            native_status="consultation",
            track="proposed",
            published_at=(utcnow() - timedelta(days=9)).date(),
        ),
    )
    db.commit()

    briefs = client.get("/").text.split("The latest filings")[1].split("How Gazette works")[0]
    assert f'/items/{dated.item.id}"' in briefs, "a dated filing must lead the briefs"
    # Dated filings come first; undated ones only fill slots left over, which is what
    # happens here because the fixture has fewer than four datable items.
    titles = re.findall(r'<h3><a href="[^"]+">(.*?)</a>', briefs, re.S)
    first_undated = next((n for n, t in enumerate(titles) if "undated" in t), len(titles))
    assert first_undated > 0, "an undated item must never lead the briefs"
    assert all("undated" not in t for t in titles[:first_undated])

    # the feed sorts the same way: datable first, in true chronological order
    feed = client.get("/api/v1/items?limit=10").json()["items"]
    first_undated = next((n for n, i in enumerate(feed) if i["published_at"] is None), len(feed))
    assert all(i["published_at"] is not None for i in feed[:first_undated])
    assert feed[0]["published_at"] is not None


def _set_scope(client, db, slugs):
    from oblag.db.models import Org

    org = db.query(Org).first()
    org.scoped_obligations = slugs
    db.commit()
    return org


def test_scope_narrows_the_reading_surfaces_but_never_the_catalog(client, seeded, db):
    """ "The obligations you're subject to" is a promise the feed's own subtitle already
    made. Ticking them narrows the feed, the deadlines and the front page together —
    but never the catalog, which is where you do the ticking."""
    from oblag.db.models import PipelineItem

    item = db.query(PipelineItem).filter_by(source_system="federal_register").first()
    assert item.obligation is not None, "fixture item should be linked to an obligation"
    mine, theirs = item.obligation.slug, "dora"
    assert mine != theirs
    _set_scope(client, db, [theirs])  # scope to something this item is NOT

    assert item.title not in client.get("/changes").text
    assert item.title not in client.get("/deadlines").text
    assert item.title not in client.get("/").text
    # the catalog still lists everything, or you could never widen the scope again
    catalog = client.get("/obligations").text
    assert mine in catalog and theirs in catalog

    _set_scope(client, db, [mine])
    assert item.title in client.get("/changes").text


def test_scope_is_announced_wherever_it_hides_something(client, seeded, db):
    """Hidden data must never be invisible: every scoped page carries a band saying so,
    with the count and a way out. The catalog page is exempt — it hides nothing."""
    _set_scope(client, db, ["dora", "gdpr"])
    for path in ("/", "/changes", "/deadlines"):
        html = client.get(path).text
        assert "Your edition" in html, path
        assert "2 obligations" in html, path
        assert 'href="/obligations"' in html, path
    assert "Your edition" not in client.get("/obligations").text


def test_empty_scope_shows_everything(client, seeded, db):
    """A fresh instance must not hide anything before anyone has chosen, and clearing
    the selection restores the whole site rather than emptying it."""
    from oblag.db.models import PipelineItem

    item = db.query(PipelineItem).filter_by(source_system="federal_register").first()
    _set_scope(client, db, [])
    assert item.title in client.get("/changes").text
    assert "Your edition" not in client.get("/changes").text


def test_saving_the_scope_ignores_unknown_slugs(client, seeded, db):
    from oblag.db.models import Org

    r = client.post(
        "/obligations/scope",
        data={"slugs": ["gdpr", "not-a-real-obligation"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    assert db.query(Org).first().scoped_obligations == ["gdpr"]


def test_json_api_is_never_scoped_behind_a_clients_back(client, seeded, db):
    """Narrowing a programmatic client's results because of a UI setting it cannot see
    would be a trap. The API only filters when asked."""
    from oblag.db.models import PipelineItem

    item = db.query(PipelineItem).filter_by(source_system="federal_register").first()
    _set_scope(client, db, ["dora"])
    ids = [i["id"] for i in client.get("/api/v1/items?limit=200").json()["items"]]
    assert item.id in ids
    assert "scope" not in client.get("/openapi.json").text.split('"/api/v1/items"')[1][:800]


def test_watchlist_can_bind_live_to_the_scope(client, seeded, db):
    """A watchlist bound to the scope follows it as it changes, so the same list is
    never maintained twice."""
    from oblag.db.models import Event, EventType, PipelineItem, Watchlist
    from oblag.notify import matches

    item = db.query(PipelineItem).filter_by(source_system="federal_register").first()
    mine = item.obligation.slug
    org = _set_scope(client, db, ["dora"])
    wl = Watchlist(name="mine", channel="rss", filters={"use_org_scope": True}, org_id=org.id)
    db.add(wl)
    db.commit()
    ev = Event(pipeline_item_id=item.id, type=EventType.state_changed, payload={})
    db.add(ev)
    db.commit()

    assert not matches(wl, ev, item), "out of scope, so out of the watchlist"
    org.scoped_obligations = [mine]
    db.commit()
    db.refresh(wl)
    assert matches(wl, ev, item), "the watchlist follows the scope without being edited"


def test_scoped_pages_never_count_what_they_do_not_show(client, seeded, db):
    """Caught in a screenshot: the feed was scoped to three EU obligations while the
    attention band beside it still announced 29 open NERC projects. Every number on a
    scoped page counts only what that page would show."""
    _set_scope(client, db, ["gdpr"])
    html = client.get("/changes").text
    band = html.split('aria-label="Needs attention"')[1].split("</section>")[0]
    assert "NERC" not in band
    for label in ("comment windows closing", "deadlines in 30 days", "awaiting outcome"):
        assert label in band


def test_front_page_coda_offers_the_scope_it_promises(client, seeded, db):
    """ "Make it your paper" is a promise only scoping keeps, and scoping was reachable
    only by wandering into the catalog. The coda leads with it, and once a scope is set
    the same button reads as an edit rather than a fresh choice."""
    coda = client.get("/").text.split('class="coda"')[1].split("</div>\n</div>")[0]
    assert 'href="/obligations"' in coda
    assert "Choose your obligations" in coda
    assert "Change what you follow" not in coda

    _set_scope(client, db, ["gdpr"])
    coda = client.get("/").text.split('class="coda"')[1].split("</div>\n</div>")[0]
    assert "Change what you follow" in coda
    # the denominator has to survive scoping, or "1 of 1" reads as nothing hidden
    assert "1 of " in coda
    assert "1 of 1 obligations" not in coda


def test_catalog_picker_is_thumb_shaped_on_mobile(client, seeded):
    """Stacked as an ordinary data cell, the "mine" checkbox rendered centred under a
    MINE heading, orphaned above the name it belongs to, in a card ~304px tall — fifty
    of those is a 15,000px scroll to choose six. On a phone the box rides beside the
    obligation name and the metadata labels run inline with their values."""
    html = client.get("/obligations").text
    assert "table.stack td.pick { float:left" in html
    assert "table.stack td.pick::before { content:none; }" in html
    # the stack rule forces every cell to 100%; without width:auto the title cannot
    # shrink to fit beside the float and drops onto its own line
    assert "table.stack td.titlecell { overflow:hidden; width:auto; }" in html
    assert "table.catalog td[data-label]::before { display:inline" in html
    assert 'class="rows stack catalog"' in html
