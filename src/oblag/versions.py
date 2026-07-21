"""Version-string comparison shared by flavor detection and version-bump suggestions.

Standards number themselves inconsistently — "4.0.1", "v5.0", "Rev. 5", "2022"
(ISO edition year), "11.8" — but within a single obligation the convention is
stable, so a comparison that extracts numeric tokens and compares them as a tuple
orders the same-family values correctly. This module is deliberately DB-free so the
ORM model, the web layer, and the suggestion engine can all import it."""

from __future__ import annotations

import re

# A version token embedded in prose needs an explicit lead-in ("v5.0", "Rev. 3",
# "Version 3.2") so a stray number in a title — "SP 800-53", "ISO/IEC 27001" — is not
# mistaken for the version. A bare numeric string ("4.0.1", "2022") is only trusted
# when it IS the whole value, which is how catalog versions are written.
_PREFIXED_RE = re.compile(r"\b(?:v|rev\.?\s*|version\s+)(\d+(?:\.\d+)*)", re.IGNORECASE)
_BARE_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*$")


def version_key(text: str | None) -> tuple[int, ...] | None:
    """Comparable key from a version string or a title containing one. Trailing zeros
    are dropped so "4.0" == "4" and "5.0" == "5". None when no version token is found."""
    if not text:
        return None
    m = _PREFIXED_RE.search(text) or _BARE_RE.match(text)
    if m is None:
        return None
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def same_version(a: str | None, b: str | None) -> bool:
    """True when both carry the same comparable version (ignoring surface form)."""
    ka, kb = version_key(a), version_key(b)
    return ka is not None and ka == kb


def is_newer(candidate: str | None, baseline: str | None) -> bool:
    """True when `candidate` parses to a strictly higher version than `baseline`.
    A missing/unparseable candidate is never newer; a candidate over a missing
    baseline is (a first known version counts as an advance)."""
    kc = version_key(candidate)
    if kc is None:
        return False
    kb = version_key(baseline)
    if kb is None:
        return True
    return kc > kb


def latest(*versions: str | None) -> str | None:
    """The version string with the highest comparable key. Unparseable/None values are
    ignored; ties and all-unparseable fall back to the first non-empty argument."""
    best: str | None = None
    best_key: tuple[int, ...] | None = None
    for v in versions:
        k = version_key(v)
        if k is not None and (best_key is None or k > best_key):
            best, best_key = v, k
    if best is not None:
        return best
    return next((v for v in versions if v), None)
