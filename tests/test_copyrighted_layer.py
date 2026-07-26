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


def _iec_raw(fixture: str, slug: str, number: str, catalog_url: str) -> RawDocument:
    return RawDocument(
        url="https://webstore-search-api.iec.ch/api/search",
        content=load_fixture("iso_catalog", fixture),
        content_type="application/json",
        meta={"obligation_slug": slug, "catalog_url": catalog_url, "number": number},
    )


def test_iso_catalog_parse_and_state(db):
    # the base standard, read from the co-publisher's search API
    items = list(
        IsoCatalogAdapter().normalize(
            _iec_raw(
                "iec_27002.json", "iso-27002", "27002", "https://www.iso.org/standard/75652.html"
            )
        )
    )
    assert len(items) == 1
    item = items[0]
    assert item.title == "ISO/IEC 27002:2022"
    assert item.native_status == "published"
    assert item.native_meta["edition"] == "3"
    assert item.native_meta["publication_date"] == "2022-02-15"
    assert item.published_at == date(2022, 2, 15)
    assert item.url == "https://webstore.iec.ch/en/publication/74287"
    # keeps the identity the iso.org-era rows carry, so the switch refreshes in place
    assert item.external_key == ("iso_project", "https://www.iso.org/standard/75652.html")
    res = reduce_item(db, item, today=date(2026, 7, 14))
    assert res.item.state is ItemState.effective


def test_iso_catalog_finds_amendments_and_drops_handbooks(db):
    # the 27001 search returns a handbook ABOUT the standard and a real amendment TO it
    items = list(
        IsoCatalogAdapter().normalize(
            _iec_raw("iec_27001.json", "iso-27001", "27001", "https://www.iso.org/standard/27001")
        )
    )
    assert len(items) == 1, "ISO/IEC 27001-HBK is a handbook, not the standard"
    amd = items[0]
    assert amd.title == "ISO/IEC 27001:2022/AMD1:2024 Amendment 1: Climate action changes"
    # its own publication, so its own key — it must not overwrite the 2022 edition
    assert amd.external_key == ("iec_pub", "92579")
    assert amd.published_at == date(2024, 2, 23)
    res = reduce_item(db, amd, today=date(2026, 7, 14))
    assert res.item.state is ItemState.effective


def test_iso_catalog_emits_nothing_for_iso_only_standards():
    """ISO 22301 is not a joint publication, so IEC returns no hits for it. Emitting a
    stub would overwrite the curated row with a placeholder; emitting nothing leaves it
    alone."""
    empty = b'{"primary": {"hits": {"hits": []}}}'
    raw = RawDocument(
        url="https://webstore-search-api.iec.ch/api/search",
        content=empty,
        meta={
            "obligation_slug": "iso-22301",
            "catalog_url": "https://www.iso.org/standard/75106.html",
            "number": "22301",
        },
    )
    assert list(IsoCatalogAdapter().normalize(raw)) == []


def test_iso_catalog_survives_malformed_payload():
    raw = RawDocument(
        url="https://webstore-search-api.iec.ch/api/search",
        content=b"<html>nope</html>",
        meta={"obligation_slug": "iso-27002", "catalog_url": "u", "number": "27002"},
    )
    assert list(IsoCatalogAdapter().normalize(raw)) == []


def test_amendment_year_does_not_become_the_standards_version():
    """ISO/IEC 27001:2022/AMD1:2024 amends the 2022 edition. Reading the version off the
    publication date would have proposed a bogus 2024 bump."""
    from oblag.versionsuggest import _published_version

    class _Fake:
        source_system = "iso_catalog"
        state = ItemState.effective
        native_status = "published"
        native_meta = {
            "reference": "ISO/IEC 27001:2022/AMD1:2024",
            "publication_date": "2024-02-23",
        }

    assert _published_version(_Fake()) == "2022"


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        # IEC publication status (the live path)
        ("published", ItemState.effective),
        ("withdrawn", ItemState.withdrawn),
        # ISO harmonized stage codes, still carried by rows ingested from iso.org
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
