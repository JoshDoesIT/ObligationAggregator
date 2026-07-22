"""Derived watch items: things pending a real-world outcome that has no announced
date — so they never appear on the deadlines list, yet are exactly what a GRC team
wants an eye on. Computed from live pipeline state on every render, never hand-kept:

- a consultation that CLOSED and hasn't been resolved by a publication → the
  standards body owes an outcome (publish / revise / drop);
- an adopted document with no effective date yet → effectiveness pending
  (e.g. NERC standards filed with FERC awaiting approval);
- an ISO edition under revision (stage 90.92 or a successor project underway) →
  a new edition is coming while the current one remains in force."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from oblag.core.reducer import current_dates
from oblag.db.models import DateType, ItemState, PipelineItem

# consultation sources whose closed windows imply a pending outcome (rulemakings from
# broad legal sources are excluded: agencies may lawfully let dockets die quietly, so
# "closed" there is not a promise of an outcome)
_CONSULTATION_SOURCES = {"pci_ssc", "have_your_say", "edpb", "esma", "eba", "aicpa", "nerc"}


def _live(db: Session, item: PipelineItem, dtype: DateType) -> Any:
    for (dt, _label), kd in current_dates(db, item.id).items():
        if dt is dtype:
            return kd.value
    return None


def pending_outcomes(db: Session) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    items = (
        db.query(PipelineItem)
        .filter(
            PipelineItem.state.in_([ItemState.comment_closed, ItemState.final_pending_effective])
        )
        .all()
    )
    for item in items:
        ob = item.obligation
        if item.state == ItemState.comment_closed:
            if item.source_system not in _CONSULTATION_SOURCES:
                continue
            if item.resolved_change_id is not None:
                continue
            if _live(db, item, DateType.adopted) is not None:
                # concluded — the outcome (an adopted act) is already recorded; the
                # state flips to effective on the next re-reduce
                continue
            if ob is not None and ob.effective_version:
                from oblag.versions import version_key

                subject = version_key(item.title)
                cur = version_key(ob.effective_version)
                if subject is not None and cur is not None and subject <= cur:
                    # the consultation's subject version is already in force: either a
                    # feedback-on-current RFC (no promised outcome) or a draft whose
                    # version has since published — nothing is pending
                    continue
            closed = _live(db, item, DateType.comment_close)
            out.append(
                {
                    "kind": "awaiting_outcome",
                    "item_id": item.id,
                    "title": item.title,
                    "obligation": ob.slug if ob else None,
                    "detail": (
                        f"Consultation closed {closed.isoformat()}; outcome pending"
                        if closed
                        else "Consultation closed; outcome pending"
                    ),
                }
            )
        elif item.state == ItemState.final_pending_effective:
            eff = _live(db, item, DateType.effective)
            if eff is not None:
                continue  # a dated effectiveness already shows on the deadlines list
            out.append(
                {
                    "kind": "adopted_pending_effective",
                    "item_id": item.id,
                    "title": item.title,
                    "obligation": ob.slug if ob else None,
                    "detail": "Adopted — effective date not yet announced",
                }
            )
    # ISO editions under revision: the item is effective (edition in force) but the
    # stage code says a revision is underway
    iso = (
        db.query(PipelineItem)
        .filter(
            PipelineItem.source_system == "iso_catalog",
            PipelineItem.native_status == "90.92",
        )
        .all()
    )
    for item in iso:
        ob = item.obligation
        out.append(
            {
                "kind": "revision_underway",
                "item_id": item.id,
                "title": item.title,
                "obligation": ob.slug if ob else None,
                "detail": "Revision underway — current edition remains in force until "
                "the new edition publishes",
            }
        )
    out.sort(key=lambda w: (w["kind"], w["title"]))
    return out
