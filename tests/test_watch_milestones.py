from __future__ import annotations

from datetime import date

from oblag.adapters.base import NormalizedDate, NormalizedItem
from oblag.catalog import seed_obligations
from oblag.core.reducer import reduce_item
from oblag.db.models import Confidence, DateType, ItemState, PipelineItem
from oblag.milestones import seed_milestones
from oblag.watch import pending_outcomes


def test_milestone_seeding_is_idempotent_and_effective(db):
    seed_obligations(db)
    seed_milestones(db)
    db.commit()
    item = db.query(PipelineItem).filter_by(source_system="curated").one()
    assert "EU AI Act" in item.title
    assert item.state == ItemState.effective  # entry into force 2024-08-01 has passed
    assert item.obligation.slug == "eu-ai-act"
    dates = {(kd.date_type, kd.label): kd for kd in item.key_dates}
    assert (
        DateType.phased_compliance,
        "high-risk AI systems (Annex III) — deferred by Digital Omnibus",
    ) in dates
    n_dates = len(item.key_dates)

    seed_milestones(db)  # re-seed: no duplicate assertions, no state churn
    db.commit()
    db.expire_all()
    item = db.query(PipelineItem).filter_by(source_system="curated").one()
    assert len(item.key_dates) == n_dates


def test_pending_outcomes_derivation(db, client):
    seed_obligations(db)
    # closed consultation, unresolved → awaiting outcome
    reduce_item(
        db,
        NormalizedItem(
            source_system="pci_ssc",
            external_key=("pci_doc", "kmo-rfc"),
            jurisdiction="Global",
            title="PCI SSC RFC: PCI KMO v1.0 Standard",
            native_status="rfc",
            track="proposed",
            obligation_slug="pci-kmo",
            dates=[
                NormalizedDate(DateType.comment_close, date(2026, 1, 9), Confidence.published_firm)
            ],
        ),
    )
    # adopted, no effective date → pending effectiveness
    reduce_item(
        db,
        NormalizedItem(
            source_system="nerc",
            external_key=("nerc_project", "2023-03"),
            jurisdiction="US-Federal",
            title="NERC Project 2023-03: Internal network security monitoring INSM",
            native_status="Board adopted and filed with FERC",
            track="proposed",
            obligation_slug="nerc-cip",
        ),
    )
    # ISO edition under revision (90.92) → revision underway
    reduce_item(
        db,
        NormalizedItem(
            source_system="iso_catalog",
            external_key=("iso_project", "iso-27017"),
            jurisdiction="Global",
            title="ISO/IEC 27017:2015",
            native_status="90.92",
            track="default",
            obligation_slug="iso-27017",
        ),
    )
    # closed FEDERAL rulemaking must NOT appear (agencies may let dockets die)
    reduce_item(
        db,
        NormalizedItem(
            source_system="federal_register",
            external_key=("fr_doc_number", "2024-99999"),
            jurisdiction="US-Federal",
            title="Some closed NPRM",
            native_status="PRORULE",
            track="proposed",
            dates=[
                NormalizedDate(DateType.comment_close, date(2024, 1, 1), Confidence.published_firm)
            ],
        ),
    )
    db.commit()

    watch = pending_outcomes(db)
    kinds = {(w["kind"], w["title"]) for w in watch}
    assert ("awaiting_outcome", "PCI SSC RFC: PCI KMO v1.0 Standard") in kinds
    assert (
        "adopted_pending_effective",
        "NERC Project 2023-03: Internal network security monitoring INSM",
    ) in kinds
    assert ("revision_underway", "ISO/IEC 27017:2015") in kinds
    assert not any(w["title"] == "Some closed NPRM" for w in watch)

    # and the deadlines page renders the panel
    html = client.get("/deadlines").text
    assert "Watching — no date announced yet" in html
    assert "PCI KMO v1.0" in html


def test_watch_excludes_concluded_and_in_force_subjects(db):
    """Not pending: a consultation with a recorded adoption, and an RFC whose subject
    version is already in force (feedback-on-current, or a since-published draft)."""
    from oblag.core.assertions import assert_date
    from oblag.db.models import Obligation

    seed_obligations(db)
    db.query(Obligation).filter_by(slug="pci-dss").update({Obligation.current_version: "4.0.1"})
    db.commit()
    # feedback-on-current RFC (subject == in force) → not pending
    reduce_item(
        db,
        NormalizedItem(
            source_system="pci_ssc",
            external_key=("pci_doc", "dss-rfc"),
            jurisdiction="Global",
            title="PCI SSC RFC: PCI DSS v4.0.1",
            native_status="rfc",
            track="proposed",
            obligation_slug="pci-dss",
            dates=[
                NormalizedDate(DateType.comment_close, date(2026, 7, 20), Confidence.published_firm)
            ],
        ),
    )
    # concluded consultation (adopted date recorded) → not pending
    reduce_item(
        db,
        NormalizedItem(
            source_system="have_your_say",
            external_key=("hys_initiative", "13410"),
            jurisdiction="EU",
            title="Cyber Resilience Act",
            native_status="ADOPTION_WORKFLOW",
            track="proposed",
            obligation_slug="eu-cra",
            dates=[
                NormalizedDate(DateType.comment_close, date(2023, 1, 23), Confidence.published_firm)
            ],
        ),
    )
    db.commit()
    cra = db.query(PipelineItem).filter_by(title="Cyber Resilience Act").one()
    assert_date(
        db,
        cra.id,
        DateType.adopted,
        date(2024, 10, 23),
        Confidence.published_firm,
        label="Regulation (EU) 2024/2847",
    )
    db.commit()
    assert pending_outcomes(db) == []


def test_boot_completes_concluded_consultations(db):
    from oblag.core.assertions import assert_date
    from oblag.maintenance import complete_concluded_consultations

    seed_obligations(db)
    reduce_item(
        db,
        NormalizedItem(
            source_system="have_your_say",
            external_key=("hys_initiative", "12527"),
            jurisdiction="EU",
            title="Requirements for Artificial Intelligence",
            native_status="ADOPTION_WORKFLOW",
            track="proposed",
            obligation_slug="eu-ai-act",
            dates=[
                NormalizedDate(DateType.comment_close, date(2021, 8, 6), Confidence.published_firm)
            ],
        ),
    )
    db.commit()
    item = db.query(PipelineItem).filter_by(title="Requirements for Artificial Intelligence").one()
    assert item.state == ItemState.comment_closed
    assert_date(
        db,
        item.id,
        DateType.adopted,
        date(2024, 6, 13),
        Confidence.published_firm,
        label="Regulation (EU) 2024/1689",
    )
    db.commit()
    assert complete_concluded_consultations(db) == 1
    db.commit()
    db.expire_all()
    assert item.state == ItemState.effective
    assert complete_concluded_consultations(db) == 0  # idempotent


def test_uae_export_rule_is_known_bad(db):
    from oblag.maintenance import purge_known_bad

    seed_obligations(db)
    reduce_item(
        db,
        NormalizedItem(
            source_system="federal_register",
            external_key=("fr_doc_number", "2026-14132"),
            jurisdiction="US-Federal",
            title="Enhanced Favorable Treatment for the United Arab Emirates Under the "
            "Export Administration Regulations",
            native_status="RULE",
            track="final",
        ),
    )
    db.commit()
    assert purge_known_bad(db) == 1
    db.commit()
    assert (
        db.query(PipelineItem).filter(PipelineItem.title.like("%United Arab Emirates%")).count()
        == 0
    )
