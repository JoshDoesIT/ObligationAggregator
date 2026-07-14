from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.iso_catalog import IsoCatalogAdapter
from oblag.adapters.pci_ssc import PciSscAdapter
from oblag.byol import ByolError, add_document, diff_versions
from oblag.catalog import seed_obligations
from oblag.core.reducer import reduce_item
from oblag.db.models import Confidence, DateType, ItemState
from oblag.structure import diff_structures, extract_requirements

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
    assert dates[DateType.comment_open].value == date(2026, 6, 3)
    close = dates[DateType.comment_close]
    assert close.value == date(2026, 6, 3) + timedelta(days=30)
    assert close.confidence is Confidence.derived  # never presented as firm


def test_pci_rfc_lifecycle(db):
    adapter = PciSscAdapter()
    raw = RawDocument(url="https://test", content=load_fixture("pci_ssc", "blog.rss"))
    (rfc,) = adapter.normalize(raw)
    res = reduce_item(db, rfc, today=date(2026, 6, 10))
    assert res.item.state is ItemState.comment_open
    from oblag.core.reducer import tick

    events = tick(db, today=date(2026, 7, 10))
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


def test_extract_requirements_line_anchored():
    reqs = extract_requirements(PCI_V1)
    assert set(reqs) == {"8.3", "8.3.6", "8.3.9", "12.1"}  # cross-reference NOT extracted
    assert reqs["8.3.6"].heading.startswith("Passwords/passphrases")


def test_diff_structures():
    diff = diff_structures(extract_requirements(PCI_V1), extract_requirements(PCI_V2))
    assert [r.identifier for r in diff.added] == ["8.3.10", "A.5.23"]
    assert [r.identifier for r in diff.removed] == ["8.3.9"]
    assert diff.kept == 3


# --- BYOL store + policy gating ---


@pytest.fixture()
def byol_files(tmp_path: Path):
    v1 = tmp_path / "std_v1.txt"
    v2 = tmp_path / "std_v2.txt"
    v1.write_text(PCI_V1)
    v2.write_text(PCI_V2)
    return v1, v2


def test_byol_requires_license_attestation(db, byol_files):
    seed_obligations(db)
    with pytest.raises(ByolError, match="license"):
        add_document(db, "pci-dss", "4.0", byol_files[0], license_attested=False)


def test_byol_diff_gated_by_display_policy(db, byol_files):
    seed_obligations(db)
    v1, v2 = byol_files
    add_document(db, "pci-dss", "4.0", v1, license_attested=True)
    add_document(db, "pci-dss", "4.0.1", v2, license_attested=True)

    # pci-dss policy is ids_and_titles → headings included
    diff = diff_versions(db, "pci-dss", "4.0", "4.0.1")
    assert diff.counts == {"added": 2, "removed": 1, "kept": 3}
    assert {"id": "8.3.10", "heading": "New MFA requirement for all access."} in diff.added

    # iso-27001 policy is ids_only → identifiers, no headings
    add_document(db, "iso-27001", "2013", v1, license_attested=True)
    add_document(db, "iso-27001", "2022", v2, license_attested=True)
    diff = diff_versions(db, "iso-27001", "2013", "2022")
    assert diff.added is not None
    assert all(set(entry) == {"id"} for entry in diff.added)

    # events_only → counts only
    from oblag.db.models import DisplayPolicy, Obligation

    ob = db.query(Obligation).filter_by(slug="iso-27001").one()
    ob.display_policy = DisplayPolicy.events_only
    db.flush()
    diff = diff_versions(db, "iso-27001", "2013", "2022")
    assert diff.added is None and diff.removed is None
    assert diff.counts["added"] == 2


def test_byol_files_live_under_private_dir_only(db, byol_files, tmp_path):
    seed_obligations(db)
    add_document(db, "pci-dss", "4.0", byol_files[0], license_attested=True)
    from oblag.config import get_settings

    private = get_settings().private_dir
    assert (private / "pci-dss" / "4.0" / "std_v1.txt").exists()
    # nothing in the shared snapshot store
    snapshots = get_settings().snapshot_dir
    assert not snapshots.exists() or not any(snapshots.rglob("*"))
