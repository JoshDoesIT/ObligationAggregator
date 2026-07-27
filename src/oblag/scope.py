"""Relevance gate: is a document about security/privacy/GRC at all?

The platform tracks security & privacy obligations (spec 00). Broad sources —
the Federal Register publishes halibut fishery adjustments and drawbridge
schedules, CELLAR every EU act, Have Your Say all DIGITAL-topic initiatives —
need a scope filter so ingestion stays on-mission.

The vocabulary is split by how much a match is worth WHERE it appears, because
matching any term anywhere let obvious noise through. Measured live against 12
suspect items: eleven had no security or privacy word in the TITLE at all and
were admitted purely on a passing mention in the abstract — halibut fishery
rules on "AI", HUD noise abatement on "surveillance", MARAD citizenship on
"personally identifiable", a futures-trading RFC on "surveillance". A rule's
title states its subject; its abstract mentions everything it touches.

So:
  * STRONG terms match in the title OR the abstract. These name a regime or a
    security fact that cannot be incidental — "ransomware", "breach
    notification", "GDPR". If a rule's abstract says it, the rule is about it.
  * WEAK terms must appear in the TITLE. Generic words that ride along in
    federal boilerplate — "privacy", "surveillance", "AI" — say what a document
    touches, not what it is about.

Still recall-tuned within that: a false positive costs a stray item, a false
negative silently misses an obligation change, so the weak list stays broad and
only loses its power to fire from body text. Terms are word-boundary matched,
case-insensitive. Deployments can extend the vocabulary with
OBLAG_SCOPE_EXTRA_TERMS (comma-separated, treated as strong) or disable the gate
entirely with OBLAG_SCOPE_FILTER=false. Adapters for inherently in-scope sources
(NIST CSRC, PCI SSC, EDPB, …) never consult the gate.
"""

from __future__ import annotations

import re
from functools import lru_cache

from oblag.config import get_settings

# Named regimes and security facts that cannot be incidental. A rule whose abstract
# says "ransomware" or "breach notification" IS about that.
STRONG_SCOPE_TERMS = [
    "cybersecurity",
    "cyber security",
    "cyber incident",
    "cyber risk",
    "cyber resilience",
    "cyber threat",
    "information security",
    "security incident",
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
    "data protection",
    "protected health information",
    "identity theft",
    "safeguards rule",
    "data broker",
    "hipaa",
    "glba",
    "fisma",
    "fedramp",
    "circia",
    "gdpr",
    "eidas",
]

# Words that say what a document TOUCHES rather than what it is about. Federal rules
# carry Privacy Act boilerplate, agencies "surveil" fisheries, and "AI" appears in
# almost every 2026 abstract. Title-only, where a word is a statement of subject.
WEAK_SCOPE_TERMS = [
    "security standard",
    "security requirement",
    "security controls",
    "security certification",
    "network security",
    "critical infrastructure",
    "critical entities",
    "incident report",
    "privacy",
    "personal data",
    "personal information",
    "personally identifiable",
    "biometric",
    "surveillance",
    "consumer data",
    "data governance",
    "data act",
    "data package",
    "electronic identification",
    "artificial intelligence",
    "ai",  # exact word only — "airworthiness" must not match
]

# Kept as the union for callers that just want the vocabulary (docs, tests, tooling).
DEFAULT_SCOPE_TERMS = STRONG_SCOPE_TERMS + WEAK_SCOPE_TERMS


def _term_re(t: str) -> str:
    # trailing '*' = open-ended prefix (cryptograph* → cryptography/-ic); everything
    # else matches the exact word plus simple inflections, both ends bounded so 'ai'
    # never matches inside 'airworthiness'
    if t.endswith("*"):
        return re.escape(t[:-1])
    return re.escape(t) + r"(?:s|es|ing)?\b"


def _compile(terms: list[str]) -> re.Pattern[str]:
    alternation = "|".join(_term_re(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?i)\b(?:{alternation})", re.UNICODE)


@lru_cache(maxsize=4)
def _scope_re(extra_terms: str) -> re.Pattern[str]:
    """Everything that may fire from body text: the strong list plus operator extras.
    Operator terms are trusted as strong — a deployment that adds one means it."""
    terms = list(STRONG_SCOPE_TERMS)
    terms.extend(t.strip() for t in extra_terms.split(",") if t.strip())
    return _compile(terms)


@lru_cache(maxsize=4)
def _title_re(extra_terms: str) -> re.Pattern[str]:
    """Everything a TITLE may fire from: the whole vocabulary, extras included. The
    extras belong on BOTH paths — an operator who adds a term means it everywhere."""
    terms = STRONG_SCOPE_TERMS + WEAK_SCOPE_TERMS
    terms.extend(t.strip() for t in extra_terms.split(",") if t.strip())
    return _compile(terms)


def in_scope(*texts: str | None) -> bool:
    """True when the document is about security/privacy — or when the gate is disabled.

    The FIRST text is treated as the title and may match the full vocabulary. The rest
    are body text and only the strong list applies. Callers pass (title, abstract,
    action); a caller with one string gets the full vocabulary, which is right because
    a single string is the only statement of subject there is."""
    settings = get_settings()
    if not settings.scope_filter:
        return True
    title, *body = texts
    if title and _title_re(settings.scope_extra_terms).search(title):
        return True
    pattern = _scope_re(settings.scope_extra_terms)
    return any(t and pattern.search(t) for t in body)
