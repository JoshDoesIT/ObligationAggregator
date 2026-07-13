"""Shipped obligation catalog: the flagship frameworks/regulations GRC engineers track.

`copyright_status` + `display_policy` drive what the UI may ever render for each
obligation (spec 00 invariant 3). Conservative defaults: ISO gets ids_only (litigious),
PCI gets ids_and_titles (SSC publishes summary-of-changes tables publicly)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from oblag.db.models import CopyrightStatus, DisplayPolicy, Obligation

CATALOG: list[dict] = [
    # --- US government works: public domain, full text allowed ---
    dict(
        slug="nist-800-53",
        name="NIST SP 800-53 (Security and Privacy Controls)",
        issuing_body="NIST",
        jurisdiction="US-Federal",
        canonical_url="https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final",
        copyright_status=CopyrightStatus.public_domain,
        display_policy=DisplayPolicy.full_text,
    ),
    dict(
        slug="nist-csf",
        name="NIST Cybersecurity Framework",
        issuing_body="NIST",
        jurisdiction="US-Federal",
        canonical_url="https://www.nist.gov/cyberframework",
        copyright_status=CopyrightStatus.public_domain,
        display_policy=DisplayPolicy.full_text,
    ),
    dict(
        slug="nist-800-171",
        name="NIST SP 800-171 (Protecting CUI)",
        issuing_body="NIST",
        jurisdiction="US-Federal",
        canonical_url="https://csrc.nist.gov/pubs/sp/800/171/r3/final",
        copyright_status=CopyrightStatus.public_domain,
        display_policy=DisplayPolicy.full_text,
    ),
    dict(
        slug="hipaa",
        name="HIPAA Security & Privacy Rules",
        issuing_body="HHS/OCR",
        jurisdiction="US-Federal",
        canonical_url="https://www.hhs.gov/hipaa",
        copyright_status=CopyrightStatus.public_domain,
        display_policy=DisplayPolicy.full_text,
    ),
    dict(
        slug="circia",
        name="CIRCIA Cyber Incident Reporting",
        issuing_body="CISA",
        jurisdiction="US-Federal",
        canonical_url="https://www.cisa.gov/topics/cyber-threats-and-advisories/information-sharing/cyber-incident-reporting-critical-infrastructure-act-2022-circia",
        copyright_status=CopyrightStatus.public_domain,
        display_policy=DisplayPolicy.full_text,
    ),
    dict(
        slug="fedramp",
        name="FedRAMP",
        issuing_body="GSA",
        jurisdiction="US-Federal",
        canonical_url="https://www.fedramp.gov",
        copyright_status=CopyrightStatus.public_domain,
        display_policy=DisplayPolicy.full_text,
    ),
    dict(
        slug="cmmc",
        name="CMMC",
        issuing_body="DoD",
        jurisdiction="US-Federal",
        canonical_url="https://dodcio.defense.gov/cmmc/",
        copyright_status=CopyrightStatus.public_domain,
        display_policy=DisplayPolicy.full_text,
    ),
    dict(
        slug="sox",
        name="Sarbanes-Oxley (ICFR)",
        issuing_body="SEC/PCAOB",
        jurisdiction="US-Federal",
        canonical_url="https://www.sec.gov",
        copyright_status=CopyrightStatus.public_domain,
        display_policy=DisplayPolicy.full_text,
    ),
    # --- US states ---
    dict(
        slug="ccpa",
        name="CCPA/CPRA + CPPA Regulations",
        issuing_body="California CPPA",
        jurisdiction="US-CA",
        canonical_url="https://cppa.ca.gov/regulations/",
        copyright_status=CopyrightStatus.public_domain,
        display_policy=DisplayPolicy.full_text,
    ),
    dict(
        slug="us-state-privacy",
        name="US State Comprehensive Privacy Laws",
        issuing_body="US state legislatures",
        jurisdiction="US-States",
        canonical_url="https://iapp.org/resources/article/us-state-privacy-legislation-tracker/",
        copyright_status=CopyrightStatus.public_domain,
        display_policy=DisplayPolicy.full_text,
    ),
    # --- EU: reusable with attribution ---
    dict(
        slug="gdpr",
        name="GDPR (Regulation 2016/679)",
        issuing_body="EU",
        jurisdiction="EU",
        canonical_url="https://eur-lex.europa.eu/eli/reg/2016/679/oj",
        copyright_status=CopyrightStatus.eu_reuse,
        display_policy=DisplayPolicy.full_text,
    ),
    dict(
        slug="dora",
        name="DORA (Regulation 2022/2554)",
        issuing_body="EU",
        jurisdiction="EU",
        canonical_url="https://eur-lex.europa.eu/eli/reg/2022/2554/oj",
        copyright_status=CopyrightStatus.eu_reuse,
        display_policy=DisplayPolicy.full_text,
    ),
    dict(
        slug="nis2",
        name="NIS2 (Directive 2022/2555)",
        issuing_body="EU",
        jurisdiction="EU",
        canonical_url="https://eur-lex.europa.eu/eli/dir/2022/2555/oj",
        copyright_status=CopyrightStatus.eu_reuse,
        display_policy=DisplayPolicy.full_text,
    ),
    dict(
        slug="eu-ai-act",
        name="EU AI Act (Regulation 2024/1689)",
        issuing_body="EU",
        jurisdiction="EU",
        canonical_url="https://eur-lex.europa.eu/eli/reg/2024/1689/oj",
        copyright_status=CopyrightStatus.eu_reuse,
        display_policy=DisplayPolicy.full_text,
    ),
    # --- Copyrighted standards: events/IDs only, never body text ---
    dict(
        slug="pci-dss",
        name="PCI DSS",
        issuing_body="PCI SSC",
        jurisdiction="Global",
        canonical_url="https://www.pcisecuritystandards.org/document_library/",
        copyright_status=CopyrightStatus.copyrighted,
        display_policy=DisplayPolicy.ids_and_titles,
    ),
    dict(
        slug="iso-27001",
        name="ISO/IEC 27001",
        issuing_body="ISO/IEC",
        jurisdiction="Global",
        canonical_url="https://www.iso.org/standard/27001",
        copyright_status=CopyrightStatus.copyrighted,
        display_policy=DisplayPolicy.ids_only,
    ),
    dict(
        slug="iso-27002",
        name="ISO/IEC 27002",
        issuing_body="ISO/IEC",
        jurisdiction="Global",
        canonical_url="https://www.iso.org/standard/75652.html",
        copyright_status=CopyrightStatus.copyrighted,
        display_policy=DisplayPolicy.ids_only,
    ),
    dict(
        slug="iso-42001",
        name="ISO/IEC 42001 (AI Management)",
        issuing_body="ISO/IEC",
        jurisdiction="Global",
        canonical_url="https://www.iso.org/standard/42001",
        copyright_status=CopyrightStatus.copyrighted,
        display_policy=DisplayPolicy.ids_only,
    ),
    dict(
        slug="soc2",
        name="SOC 2 Trust Services Criteria",
        issuing_body="AICPA",
        jurisdiction="Global",
        canonical_url="https://www.aicpa-cima.com/topic/audit-assurance/audit-and-assurance-greater-than-soc-2",
        copyright_status=CopyrightStatus.copyrighted,
        display_policy=DisplayPolicy.ids_and_titles,
    ),
]


def seed_obligations(session: Session) -> int:
    count = 0
    for entry in CATALOG:
        existing = session.query(Obligation).filter_by(slug=entry["slug"]).one_or_none()
        if existing is None:
            session.add(Obligation(**entry))
        else:
            for key, value in entry.items():
                setattr(existing, key, value)
        count += 1
    session.flush()
    return count
