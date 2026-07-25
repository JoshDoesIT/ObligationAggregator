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
    nav = _nav(client.get("/").text)
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


def test_landing_page_leads_with_needs_attention(client, seeded):
    html = client.get("/").text
    assert 'aria-label="Needs attention"' in html
    for label in ("comment windows closing", "deadlines in 30 days", "awaiting outcome"):
        assert label in html
    # the attention band replaced the passive count tiles
    assert 'class="stats"' not in html


def test_feed_rows_are_three_columns(client, seeded):
    """Kind and Source no longer own columns — they ride a muted subtitle so the row
    scans Change → State → Key dates."""
    html = client.get("/").text
    header = re.search(r"<table class=\"rows feed\">.*?<thead>(.*?)</thead>", html, re.S)
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
    for path in ("/", "/obligations", "/deadlines", "/watchlists", "/events", "/health"):
        assert client.get(path).status_code == 200, path


def test_byol_routes_are_gone(client, seeded):
    """The bring-your-own-license upload/diff surface was removed in v0.9.0: its
    identifier diff could not be made dependable across obligation families (a real
    NIST SP 800-171 r2→r3 comparison reported 136 added / 137 removed for a revision
    that actually carried 128 controls forward, purely because of renumbering)."""
    for path in ("/byol", "/byol/upload", "/byol/diff"):
        assert client.get(path).status_code == 404, path
