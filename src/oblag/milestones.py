"""Curated milestone timelines: application/phase dates of adopted acts that no
machine-readable feed carries (an act's phased deadlines live in the OJ text, and
amendments like the 2026 Digital Omnibus move them). Seeded at boot through the
ordinary reducer, so they get items, events, deadlines, ICS export and watchlists
like any other signal — append-only dates keep re-seeding idempotent, and a value
edit here supersedes the old assertion with a date_changed event."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from oblag.adapters.base import NormalizedDate, NormalizedItem
from oblag.db.models import Confidence, DateType

_FIRM = Confidence.published_firm

# One entry per timeline item. Dates: (type, value, label). Sources in `note`.
CURATED_MILESTONES: list[dict] = [
    {
        "key": "eu-ai-act-timeline",
        "title": "EU AI Act (Regulation 2024/1689) — application timeline",
        "obligation": "eu-ai-act",
        "jurisdiction": "EU",
        "url": "https://eur-lex.europa.eu/eli/reg/2024/1689/oj",
        "abstract": (
            "Key application dates of the EU AI Act, including the deferrals enacted by "
            "the Digital Omnibus (June 2026): high-risk (Annex III) obligations move to "
            "2 December 2027 and AI in regulated products (Annex I) to 2 August 2028. "
            "GPAI and governance obligations apply since 2 August 2025."
        ),
        "dates": [
            (DateType.entry_into_force, date(2024, 8, 1), None),
            (DateType.application, date(2025, 2, 2), "prohibitions + AI literacy"),
            (DateType.application, date(2025, 8, 2), "GPAI obligations + governance"),
            (
                DateType.phased_compliance,
                date(2027, 12, 2),
                "high-risk AI systems (Annex III) — deferred by Digital Omnibus",
            ),
            (
                DateType.phased_compliance,
                date(2028, 8, 2),
                "AI in regulated products (Annex I) — deferred by Digital Omnibus",
            ),
        ],
    },
]


def seed_milestones(session: Session) -> int:
    from oblag.core.reducer import reduce_item

    for entry in CURATED_MILESTONES:
        reduce_item(
            session,
            NormalizedItem(
                source_system="curated",
                external_key=("curated_timeline", entry["key"]),
                jurisdiction=entry["jurisdiction"],
                title=entry["title"],
                abstract=entry.get("abstract"),
                url=entry.get("url"),
                native_status="timeline",
                track="final",
                obligation_slug=entry["obligation"],
                dates=[
                    NormalizedDate(dtype, value, _FIRM, label=label)
                    for dtype, value, label in entry["dates"]
                ],
            ),
        )
    return len(CURATED_MILESTONES)
