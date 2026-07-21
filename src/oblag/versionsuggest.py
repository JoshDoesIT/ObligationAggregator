"""Automatic version tracking: advance each standard's in-force version when a newer
one is published, using the change signals we already ingest — no human step.

Reliability comes from what we DON'T auto-apply. Only genuine *publication* signals
(never drafts/RFCs) are considered; the new version must parse cleanly and be a
*plausible* forward step (see versions.plausible_successor). An implausible jump — the
fingerprint of a mis-parse — is recorded as flagged and left for a catalog edit, never
silently installed. A catalog edit always wins (the sync clears the auto value)."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from oblag.db.models import ItemState, Obligation, PipelineItem, VersionDecision
from oblag.versions import is_newer, latest, plausible_successor, version_key


def _published_version(item: PipelineItem) -> str | None:
    """The version string a publication signal asserts as newly in force, or None when
    the item isn't a publication signal. Kept per-source: each body announces
    publications differently, and only *final* signals (never drafts/RFCs) qualify."""
    meta = item.native_meta or {}
    if item.source_system == "pci_ssc" and item.native_status == "publication":
        return meta.get("published_version")
    if item.source_system == "iso_catalog" and item.state == ItemState.effective:
        # an ISO edition's version IS its publication year
        m = re.match(r"(\d{4})", meta.get("publication_date") or "")
        return m.group(1) if m else None
    return None


def _newest_candidate(ob: Obligation) -> tuple[str, PipelineItem] | None:
    """The newest published version among this obligation's ingested items, if any."""
    best: tuple[str, PipelineItem] | None = None
    for it in ob.items:
        pv = _published_version(it)
        if pv is None:
            continue
        if best is None or is_newer(pv, best[0]):
            best = (pv, it)
    return best


def _versioned(db: Session) -> list[Obligation]:
    return (
        db.query(Obligation)
        .filter(
            (Obligation.current_version.isnot(None)) | (Obligation.confirmed_version.isnot(None))
        )
        .all()
    )


def _record(db: Session, ob: Obligation, version: str, decision: str, item_id: int | None) -> None:
    row = db.query(VersionDecision).filter_by(obligation_id=ob.id, version=version).one_or_none()
    if row is None:
        db.add(
            VersionDecision(
                obligation_id=ob.id,
                version=version,
                decision=decision,
                source_item_id=item_id,
                decided_by="auto",
            )
        )
    else:
        row.decision, row.source_item_id = decision, item_id


def auto_apply(db: Session) -> list[dict[str, Any]]:
    """Advance every obligation to the newest plausibly-newer published version detected
    in the feed. Idempotent: a version already ruled on (applied or flagged) is skipped,
    so re-running never double-acts. Implausible candidates are flagged, not applied.
    Returns the actions taken this pass. Safe to call after every ingestion run."""
    ruled = {(d.obligation_id, version_key(d.version)) for d in db.query(VersionDecision).all()}
    actions: list[dict[str, Any]] = []
    for ob in _versioned(db):
        cand = _newest_candidate(ob)
        if cand is None:
            continue
        version, item = cand
        if not is_newer(version, ob.effective_version):
            continue
        if (ob.id, version_key(version)) in ruled:
            continue
        applied = plausible_successor(ob.effective_version, version)
        if applied:
            ob.confirmed_version = latest(ob.confirmed_version, version)
        _record(db, ob, version, "auto" if applied else "flagged", item.id)
        actions.append({"slug": ob.slug, "version": version, "applied": applied})
    db.commit()
    return actions


def version_log(db: Session, limit: int = 100) -> list[dict[str, Any]]:
    """Recent automatic version decisions, newest first — the audit trail for the UI."""
    rows = (
        db.query(VersionDecision)
        .order_by(VersionDecision.decided_at.desc(), VersionDecision.id.desc())
        .limit(limit)
        .all()
    )
    obl = {o.id: o for o in db.query(Obligation).all()}
    items = {
        i.id: i
        for i in db.query(PipelineItem).filter(
            PipelineItem.id.in_([r.source_item_id for r in rows if r.source_item_id])
        )
    }
    out: list[dict[str, Any]] = []
    for r in rows:
        ob = obl.get(r.obligation_id)
        it = items.get(r.source_item_id) if r.source_item_id else None
        out.append(
            {
                "slug": ob.slug if ob else str(r.obligation_id),
                "name": ob.name if ob else "",
                "in_force": ob.effective_version if ob else None,
                "version": r.version,
                "decision": r.decision,
                "item_id": r.source_item_id,
                "source_url": it.url if it else None,
                "item_title": it.title if it else None,
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            }
        )
    return out
