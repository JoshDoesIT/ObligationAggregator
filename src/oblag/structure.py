"""Identifier-level structure extraction (spec 06 layer 3).

Control/requirement identifiers are facts, not copyrightable expression. Extraction is
line-anchored: an ID counts only when it starts a line (heading position), which keeps
cross-references in body text from polluting the structure."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Heading-position identifier patterns
_PATTERNS = [
    re.compile(r"^(?P<id>A\.\d{1,2}(?:\.\d{1,2})?)\b[.:)\s]"),  # ISO Annex A
    re.compile(r"^(?P<id>(?:CC|PI|A|C|P)\d{1,2}\.\d{1,2})\b[.:)\s]"),  # AICPA TSC
    re.compile(r"^(?P<id>\d{1,2}(?:\.\d{1,2}){1,3})\b[.:)\s]"),  # PCI / numeric outline
]


@dataclass(frozen=True)
class Requirement:
    identifier: str
    heading: str  # first line only — never body text


def extract_requirements(text: str) -> dict[str, Requirement]:
    """Map identifier → Requirement from document text."""
    found: dict[str, Requirement] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) > 300:
            continue
        for pattern in _PATTERNS:
            m = pattern.match(stripped)
            if m:
                ident = m.group("id")
                heading = stripped[m.end() :].strip()[:120]
                # first occurrence wins (later mentions are usually cross-references)
                if ident not in found:
                    found[ident] = Requirement(identifier=ident, heading=heading)
                break
    return found


@dataclass
class StructureDiff:
    added: list[Requirement]
    removed: list[Requirement]
    kept: int


def diff_structures(old: dict[str, Requirement], new: dict[str, Requirement]) -> StructureDiff:
    added = sorted((new[i] for i in new.keys() - old.keys()), key=lambda r: _sort_key(r.identifier))
    removed = sorted(
        (old[i] for i in old.keys() - new.keys()), key=lambda r: _sort_key(r.identifier)
    )
    return StructureDiff(added=added, removed=removed, kept=len(new.keys() & old.keys()))


def _sort_key(identifier: str) -> tuple:
    parts = re.split(r"[.\s]", identifier)
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts if p)
