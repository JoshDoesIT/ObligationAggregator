from __future__ import annotations

from datetime import date, timedelta

import pytest

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.iso_catalog import IsoCatalogAdapter
from oblag.adapters.pci_ssc import PciSscAdapter
from oblag.core.reducer import reduce_item
from oblag.db.models import Confidence, DateType, ItemState


def _org(db):
    from oblag.auth import get_default_org

    return get_default_org(db).id


# --- PCI SSC ---


def test_pci_rfc_extraction_from_live_feed():
    adapter = PciSscAdapter()
    raw = RawDocument(url="https://test", content=load_fixture("pci_ssc", "blog.rss"))
    items = list(adapter.normalize(raw))
    # the live feed contains exactly one formal RFC signal; blog noise is dropped
    assert len(items) == 1
    rfc = items[0]
    assert rfc.title.startswith("PCI SSC RFC: PCI Data Security Standard")
    assert rfc.obligation_slug == "pci-dss"
    assert rfc.native_status == "rfc"
    dates = {d.date_type: d for d in rfc.dates}
    # the announcement body states the real window: "From 3 June to 20 July"
    opened = dates[DateType.comment_open]
    assert opened.value == date(2026, 6, 3)
    assert opened.confidence is Confidence.published_firm
    close = dates[DateType.comment_close]
    assert close.value == date(2026, 7, 20)
    assert close.confidence is Confidence.published_firm


def _rfc_feed(pub_date: str, description: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item>
<title>Request for Comments: PCI Key Management Operations (KMO) v1.0 Standard</title>
<link>https://blog.pcisecuritystandards.org/x</link>
<pubDate>{pub_date}</pubDate>
<description>{description}</description>
</item>
</channel></rss>""".encode()


def test_pci_rfc_window_year_rollover():
    # "From 24 November to 9 January" announced in November closes the NEXT year
    raw = RawDocument(
        url="https://test",
        content=_rfc_feed(
            "Mon, 24 Nov 2025 12:00:00 GMT",
            "From 24 November to 9 January, eligible stakeholders are invited…",
        ),
    )
    (rfc,) = PciSscAdapter().normalize(raw)
    dates = {d.date_type: d for d in rfc.dates}
    assert dates[DateType.comment_open].value == date(2025, 11, 24)
    assert dates[DateType.comment_close].value == date(2026, 1, 9)


def test_pci_rfc_without_window_falls_back_to_derived_floor():
    raw = RawDocument(
        url="https://test",
        content=_rfc_feed("Tue, 3 Jun 2026 12:00:00 GMT", "no window stated here"),
    )
    (rfc,) = PciSscAdapter().normalize(raw)
    dates = {d.date_type: d for d in rfc.dates}
    assert dates[DateType.comment_open].value == date(2026, 6, 3)
    close = dates[DateType.comment_close]
    assert close.value == date(2026, 6, 3) + timedelta(days=30)
    assert close.confidence is Confidence.derived  # floor only — never presented as firm


def test_pci_rfc_lifecycle(db):
    adapter = PciSscAdapter()
    raw = RawDocument(url="https://test", content=load_fixture("pci_ssc", "blog.rss"))
    (rfc,) = adapter.normalize(raw)
    res = reduce_item(db, rfc, today=date(2026, 6, 10))
    assert res.item.state is ItemState.comment_open
    from oblag.core.reducer import tick

    # still open on 10 July (real window runs to 20 July), closes on the 21st
    assert tick(db, today=date(2026, 7, 10)) == []
    events = tick(db, today=date(2026, 7, 21))
    assert [e.payload["to"] for e in events] == ["comment_closed"]


# --- ISO catalog ---


def test_iso_catalog_parse_and_state(db):
    adapter = IsoCatalogAdapter()
    raw = RawDocument(
        url="https://www.iso.org/standard/27001",
        content=load_fixture("iso_catalog", "iso_27001.html"),
        content_type="text/html",
        meta={"obligation_slug": "iso-27001", "catalog_url": "https://www.iso.org/standard/27001"},
    )
    items = list(adapter.normalize(raw))
    assert len(items) == 1
    item = items[0]
    assert item.native_status == "60.60"
    assert "27001" in item.title
    assert item.native_meta["edition"] == "3"
    assert item.native_meta["publication_date"].startswith("2022")
    res = reduce_item(db, item, today=date(2026, 7, 14))
    assert res.item.state is ItemState.effective


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("30.60", ItemState.proposed),
        ("40.20", ItemState.comment_open),  # DIS ballot
        ("40.60", ItemState.comment_closed),
        ("50.20", ItemState.final_pending_effective),  # FDIS ballot
        ("60.60", ItemState.effective),
        ("90.92", ItemState.effective),
        ("95.99", ItemState.withdrawn),
    ],
)
def test_iso_stage_map(stage, expected):
    from oblag.core.statemap import compute_state

    assert compute_state("iso_catalog", stage, {}, {}, date(2026, 1, 1)) is expected


def test_iso_unknown_stage_is_anomaly():
    from oblag.core.statemap import compute_state

    assert compute_state("iso_catalog", "unknown", {}, {}, date(2026, 1, 1)) is None


# --- structure extraction ---

PCI_V1 = """\
8.3 Strong authentication is established.
8.3.6 Passwords/passphrases meet minimum complexity.
8.3.9 Passwords are changed periodically.
12.1 Information security policy.
See requirement 8.3.6 for details.
"""

PCI_V2 = """\
8.3 Strong authentication is established.
8.3.6 Passwords/passphrases meet minimum complexity.
8.3.10 New MFA requirement for all access.
12.1 Information security policy.
A.5.23 Information security for use of cloud services
"""
