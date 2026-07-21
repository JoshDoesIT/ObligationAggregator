"""Version-bump suggestions: detect that a newer version of a standard has been
published (from the change signals we already ingest) and let an operator confirm the
advance with one click, instead of hand-editing the catalog.

Detection is deliberately conservative and human-gated — a mis-parsed title only ever
produces a *suggestion*, never a silent change to the user-visible in-force version."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from oblag.db.models import ItemState, Obligation, PipelineItem, VersionDecision
from oblag.versions import is_newer, latest, version_key


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


def pending_suggestions(db: Session) -> list[dict[str, Any]]:
    """Obligations whose ingested publication signals point to a version newer than the
    one in force, excluding versions an operator has already ruled on. One row per
    obligation (its newest un-actioned candidate), newest-first by detection."""
    decided = {(d.obligation_id, version_key(d.version)) for d in db.query(VersionDecision).all()}
    out: list[dict[str, Any]] = []
    obligations = (
        db.query(Obligation)
        .filter(
            (Obligation.current_version.isnot(None)) | (Obligation.confirmed_version.isnot(None))
        )
        .all()
    )
    for ob in obligations:
        eff = ob.effective_version
        best: dict[str, Any] | None = None
        for it in ob.items:
            pv = _published_version(it)
            if pv is None or not is_newer(pv, eff):
                continue
            if (ob.id, version_key(pv)) in decided:
                continue
            if best is None or is_newer(pv, best["version"]):
                best = {"version": pv, "item": it}
        if best is not None:
            out.append(
                {
                    "obligation_id": ob.id,
                    "slug": ob.slug,
                    "name": ob.name,
                    "in_force": eff,
                    "version": best["version"],
                    "item_id": best["item"].id,
                    "item_title": best["item"].title,
                    "source_url": best["item"].url,
                }
            )
    out.sort(key=lambda s: (s["slug"],))
    return out


def _record(
    db: Session, ob: Obligation, version: str, decision: str, item_id: int | None, by: str | None
) -> None:
    row = db.query(VersionDecision).filter_by(obligation_id=ob.id, version=version).one_or_none()
    if row is None:
        db.add(
            VersionDecision(
                obligation_id=ob.id,
                version=version,
                decision=decision,
                source_item_id=item_id,
                decided_by=by,
            )
        )
    else:
        row.decision, row.source_item_id, row.decided_by = decision, item_id, by


def accept(
    db: Session, obligation_id: int, version: str, item_id: int | None, by: str | None
) -> None:
    """Confirm a bump: advance confirmed_version (forward-only) and log the decision.
    confirmed_version is untouched by the catalog sync, so the advance survives deploys."""
    ob = db.get(Obligation, obligation_id)
    if ob is None:
        raise ValueError(f"unknown obligation id {obligation_id}")
    ob.confirmed_version = latest(ob.confirmed_version, version)
    _record(db, ob, version, "accepted", item_id, by)
    db.commit()


def dismiss(
    db: Session, obligation_id: int, version: str, item_id: int | None, by: str | None
) -> None:
    """Reject a suggested bump so it never reappears. Leaves the in-force version as-is."""
    ob = db.get(Obligation, obligation_id)
    if ob is None:
        raise ValueError(f"unknown obligation id {obligation_id}")
    _record(db, ob, version, "dismissed", item_id, by)
    db.commit()
