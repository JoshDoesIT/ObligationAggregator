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
        "title": "EU AI Act (Regulation 2024/1689): application timeline",
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
    {
        "key": "pipeda-timeline",
        "title": "PIPEDA: phase-in and the 2026 data-mobility amendment",
        "obligation": "pipeda",
        "jurisdiction": "CA",
        "url": "https://laws-lois.justice.gc.ca/eng/acts/P-8.6/",
        "abstract": (
            "PIPEDA phased in over three years and remains Canada's federal "
            "private-sector privacy statute. Bill C-27 (which would have replaced it "
            "with the Consumer Privacy Protection Act) died on the Order Paper when "
            "Parliament was prorogued on 6 January 2025 and was never reintroduced. "
            "The live change is Bill C-15, the Budget 2025 Implementation Act No. 1, "
            "which received Royal Assent on 26 March 2026 and added a data-mobility "
            "framework — the first federal data-portability right in Canadian law. It "
            "is NOT yet in force: it commences on a day fixed by order of the Governor "
            "in Council, and only once sector-specific regulations are made, so no "
            "compliance date can be stated yet."
        ),
        "dates": [
            (DateType.entry_into_force, date(2001, 1, 1), "federal works and undertakings"),
            (DateType.application, date(2002, 1, 1), "health information"),
            (DateType.application, date(2004, 1, 1), "all commercial activity"),
            (
                DateType.adopted,
                date(2026, 3, 26),
                "data-mobility framework (Bill C-15) — Royal Assent, not yet in force",
            ),
        ],
    },
    {
        "key": "lgpd-timeline",
        "title": "LGPD (Lei 13.709/2018): in force, sanctions, and ANPD regulation",
        "obligation": "lgpd",
        "jurisdiction": "BR",
        "url": "https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709.htm",
        "abstract": (
            "Brazil's General Data Protection Law came into force on 18 September 2020, "
            "with administrative sanctions enforceable from 1 August 2021. The ANPD "
            "became an independent regulatory agency in 2025, and issued Resolution "
            "CD/ANPD 19/2024 on international transfers. Its 2026-2027 priority map "
            "names data-subject rights, children's data and AI as enforcement focus."
        ),
        "dates": [
            (DateType.entry_into_force, date(2020, 9, 18), None),
            (DateType.application, date(2021, 8, 1), "administrative sanctions enforceable"),
            (
                DateType.adopted,
                date(2024, 8, 23),
                "Resolution CD/ANPD 19/2024 — international transfers",
            ),
        ],
    },
    {
        "key": "us-state-privacy-timeline",
        "title": "US state comprehensive privacy laws: what takes effect when",
        "obligation": "us-state-privacy",
        "jurisdiction": "US-States",
        "url": "https://iapp.org/resources/article/us-state-privacy-legislation-tracker/",
        "abstract": (
            "Twenty states have enacted comprehensive consumer privacy laws. The dates "
            "below are the ones still ahead or newly passed; earlier states (California, "
            "Virginia, Colorado, Connecticut, Utah and the rest) are already in force. "
            "Per-bill tracking needs the LegiScan adapter, which is off until "
            "OBLAG_LEGISCAN_API_KEY and OBLAG_LEGISCAN_STATES are set."
        ),
        "dates": [
            (DateType.application, date(2026, 1, 1), "Indiana, Kentucky and Rhode Island"),
            (
                DateType.application,
                date(2027, 1, 1),
                "Oklahoma Consumer Data Privacy Act (signed 20 March 2026)",
            ),
            (
                DateType.application,
                date(2027, 5, 1),
                "Alabama Personal Data Protection Act, HB351 (signed 17 April 2026)",
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
