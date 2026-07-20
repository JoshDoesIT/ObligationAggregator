"""Fallback obligation linking: infer the catalog obligation from an item's title.

Adapters set obligation_slug when their source states it structurally (CELEX ids,
NIST series numbers, catalog page identity). Many signals only reveal their subject
in the title — "Draft Commission guidance on the Cyber Resilience Act" — so the
reducer falls back to these rules when an adapter didn't link.

Rules are deliberately conservative: a named regime, not a topic. A false link
misfiles a signal under the wrong obligation (worse than no link), so patterns
require the regime's proper name or unambiguous statutory citation.
"""

from __future__ import annotations

import re

# ordered: first match wins; longer/more specific names before shorter ones
_RULES: list[tuple[str, str]] = [
    (r"cyber resilience act", "eu-cra"),
    (r"artificial intelligence act|\bai act\b|ai regulatory sandbox", "eu-ai-act"),
    (r"digital operational resilience|\bdora\b", "dora"),
    (r"\bnis ?2\b|network and information security directive", "nis2"),
    (r"\bgdpr\b|general data protection regulation", "gdpr"),
    (r"\beidas\b|electronic identification and trust services", "eidas2"),
    (r"cyber incident reporting for critical infrastructure|\bcircia\b", "circia"),
    (r"\bhipaa\b|health insurance portability", "hipaa"),
    (r"\bfedramp\b", "fedramp"),
    (r"cybersecurity maturity model certification|\bcmmc\b", "cmmc"),
    (r"gramm.leach.bliley|glba safeguards|standards for safeguarding customer", "glba-safeguards"),
    (r"children'?s online privacy protection|\bcoppa\b", "coppa"),
    (r"\bnydfs\b|23 nycrr (part )?500", "nydfs-500"),
    (r"payment card industry data security|\bpci dss\b", "pci-dss"),
    (r"pci pts hsm|pci.*hardware security module", "pci-pts-hsm"),
    (r"pci pts poi", "pci-pts-poi"),
    (r"pci pin security", "pci-pin"),
    (r"point.to.point encryption|pci p2pe", "pci-p2pe"),
    (r"pci 3ds", "pci-3ds"),
    (r"secure software lifecycle|secure slc", "pci-secure-slc"),
    (r"pci secure software", "pci-secure-software"),
    (r"pci card production|card production and provisioning", "pci-card-production"),
    (r"pci.*token service provider", "pci-tsp"),
    (r"mobile payments? on cots|pci mpoc", "pci-mpoc"),
    (r"key management operations", "pci-kmo"),
    (r"california consumer privacy|\bccpa\b|\bcpra\b", "ccpa"),
    (r"critical security controls|cis controls", "cis-controls"),
    (r"\bhitrust\b", "hitrust-csf"),
    (r"\bnerc\b|critical infrastructure protection reliability", "nerc-cip"),
    (r"iso[/ ]?(iec )?27001", "iso-27001"),
    (r"iso[/ ]?(iec )?27701", "iso-27701"),
    (r"iso[/ ]?(iec )?42001", "iso-42001"),
    (r"iso[/ ]?(iec )?22301", "iso-22301"),
    (r"privacy framework", "nist-privacy-framework"),
    (r"sp 800-53\b", "nist-800-53"),
    (r"sp 800-171\b", "nist-800-171"),
    (r"sp 800-63\b", "nist-800-63"),
    (r"fips 140-3", "fips-140-3"),
    (r"cybersecurity framework|\bcsf 2\.0\b", "nist-csf"),
]

_COMPILED = [(re.compile(rf"(?i){pat}"), slug) for pat, slug in _RULES]


def infer_obligation(*texts: str | None) -> str | None:
    """First matching catalog slug for any of the given texts, or None."""
    for text in texts:
        if not text:
            continue
        for pattern, slug in _COMPILED:
            if pattern.search(text):
                return slug
    return None
