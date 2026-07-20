"""Relevance gate: is a document about security/privacy/GRC at all?

The platform tracks security & privacy obligations (spec 00). Broad sources —
the Federal Register publishes halibut fishery adjustments and drawbridge
schedules, CELLAR every EU act, Have Your Say all DIGITAL-topic initiatives —
need a scope filter so ingestion stays on-mission.

Recall-tuned by design: a false positive costs a stray item; a false negative
silently misses an obligation change. Terms are word-boundary matched,
case-insensitive. Deployments can extend the vocabulary with
OBLAG_SCOPE_EXTRA_TERMS (comma-separated) or disable the gate entirely with
OBLAG_SCOPE_FILTER=false (e.g. a self-hosted instance tracking other domains).
Adapters for inherently in-scope sources (NIST CSRC, PCI SSC, EDPB, NERC, …)
never consult the gate.
"""

from __future__ import annotations

import re
from functools import lru_cache

from oblag.config import get_settings

DEFAULT_SCOPE_TERMS = [
    # security
    "cybersecurity",
    "cyber security",
    "cyber incident",
    "cyber risk",
    "cyber resilience",
    "cyber threat",
    "information security",
    "security incident",
    "security standard",
    "security requirement",
    "security controls",
    "security certification",
    "network security",
    "critical infrastructure",
    "critical entities",
    "incident report",
    "breach notification",
    "data breach",
    "ransomware",
    "malware",
    "encryption",
    "cryptograph*",  # cryptography/cryptographic
    "vulnerability disclosure",
    "secure software",
    "zero trust",
    "digital operational resilience",
    "ict risk",
    "network and information",  # NIS/NIS2 phrasing
    # privacy / data protection
    "privacy",
    "data protection",
    "personal data",
    "personal information",
    "personally identifiable",
    "protected health information",
    "biometric",
    "surveillance",
    "data broker",
    "consumer data",
    "identity theft",
    "safeguards rule",
    "data governance",
    "data act",
    "data package",
    # named regimes commonly referenced in titles
    "hipaa",
    "glba",
    "fisma",
    "fedramp",
    "circia",
    "gdpr",
    "eidas",
    "electronic identification",
    "artificial intelligence",
    "ai",  # exact word only — "airworthiness" must not match
]


@lru_cache(maxsize=4)
def _scope_re(extra_terms: str) -> re.Pattern[str]:
    terms = list(DEFAULT_SCOPE_TERMS)
    terms.extend(t.strip() for t in extra_terms.split(",") if t.strip())

    def term_re(t: str) -> str:
        # trailing '*' = open-ended prefix (cryptograph* → cryptography/-ic);
        # everything else matches the exact word plus simple inflections, both
        # ends bounded so 'ai' never matches inside 'airworthiness'
        if t.endswith("*"):
            return re.escape(t[:-1])
        return re.escape(t) + r"(?:s|es|ing)?\b"

    alternation = "|".join(term_re(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?i)\b(?:{alternation})", re.UNICODE)


def in_scope(*texts: str | None) -> bool:
    """True when any text matches the vocabulary — or when the gate is disabled."""
    settings = get_settings()
    if not settings.scope_filter:
        return True
    pattern = _scope_re(settings.scope_extra_terms)
    return any(t and pattern.search(t) for t in texts)
