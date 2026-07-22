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
    if item.source_system == "hitrust" and item.native_status == "release":
        return meta.get("published_version")
    if item.source_system == "iso_catalog" and item.state == ItemState.effective:
        # an ISO edition's version IS its publication year
        m = re.match(r"(\d{4})", meta.get("publication_date") or "")
        return m.group(1) if m else None
    return None


def _candidates_newest_first(ob: Obligation) -> list[tuple[str, PipelineItem]]:
    """All published versions among this obligation's ingested items, newest first.
    The full list matters: if the newest is implausible (flagged), a smaller but
    plausible advance behind it must still get applied."""
    found: dict[tuple[int, ...], tuple[str, PipelineItem]] = {}
    for it in ob.items:
        pv = _published_version(it)
        if pv is None:
            continue
        k = version_key(pv)
        if k is not None:
            found.setdefault(k, (pv, it))
    return [found[k] for k in sorted(found, reverse=True)]


def _versioned(db: Session, only_ids: set[int] | None = None) -> list[Obligation]:
    q = db.query(Obligation).filter(
        (Obligation.current_version.isnot(None)) | (Obligation.confirmed_version.isnot(None))
    )
    if only_ids is not None:
        # scope to the obligations an ingestion run actually touched — the version pass
        # runs after every adapter, so a full 50-obligation scan each time is wasteful
        q = q.filter(Obligation.id.in_(only_ids))
    return q.all()


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


def auto_apply(db: Session, only_ids: set[int] | None = None) -> list[dict[str, Any]]:
    """Advance every obligation to the newest plausibly-newer published version detected
    in the feed. Idempotent: a version already ruled on (applied or flagged) is skipped,
    so re-running never double-acts. Implausible candidates are flagged, not applied.
    only_ids scopes the pass to obligations a run touched. Safe after every ingestion run."""
    ruled = {(d.obligation_id, version_key(d.version)) for d in db.query(VersionDecision).all()}
    actions: list[dict[str, Any]] = []
    for ob in _versioned(db, only_ids):
        # walk newest → oldest: apply the newest plausible advance; flag implausible
        # ones along the way WITHOUT letting them block a real advance behind them
        for version, item in _candidates_newest_first(ob):
            if not is_newer(version, ob.effective_version):
                break  # sorted: nothing older can be newer than in-force
            if (ob.id, version_key(version)) in ruled:
                continue
            applied = plausible_successor(ob.effective_version, version)
            if applied:
                ob.confirmed_version = latest(ob.confirmed_version, version)
            _record(db, ob, version, "auto" if applied else "flagged", item.id)
            actions.append({"slug": ob.slug, "version": version, "applied": applied})
            if applied:
                break  # in-force advanced; older candidates are now behind it
    db.commit()
    return actions


def resolve_concluded_consultations(db: Session, only_ids: set[int] | None = None) -> list[Any]:
    """Mark consultations as superseded once the version they drafted is published.

    A proposed-track RFC/draft whose subject version matches an EFFECTIVE publication
    item on the same obligation has concluded — leaving it "comment closed" forever
    implies the consultation is still pending an outcome. Time-order guard: the
    publication must postdate the consultation's opening, so an RFC soliciting
    feedback ON the current version (whose subject equals a version published long
    before it) is never claimed to be "resolved" by that older publication."""
    from oblag.core.reducer import current_dates
    from oblag.db.models import DateType, Event, EventType

    def _live_date(item: PipelineItem, dtype: DateType) -> Any:
        # current_dates keys are (date_type, label) tuples — match on type alone
        for (dt, _label), kd in current_dates(db, item.id).items():
            if dt is dtype:
                return kd.value
        return item.first_seen_at.date() if item.first_seen_at else None

    events: list[Any] = []
    for ob in _versioned(db, only_ids):
        pubs = [
            (version_key(pv), it)
            for pv, it in ((_published_version(it), it) for it in ob.items)
            if pv is not None and it.state == ItemState.effective and it.track == "final"
        ]
        if not pubs:
            continue
        for cand in ob.items:
            if (
                cand.track != "proposed"
                or cand.state not in (ItemState.comment_open, ItemState.comment_closed)
                or cand.resolved_change_id is not None
            ):
                continue
            subject = version_key(cand.title)
            if subject is None:
                continue
            for pub_key, pub in pubs:
                if pub_key != subject:
                    continue
                pub_date = _live_date(pub, DateType.effective)
                opened = _live_date(cand, DateType.comment_open)
                if pub_date is None or opened is None or pub_date < opened:
                    continue  # publication predates the consultation — not its outcome
                old_state = cand.state
                cand.resolved_change_id = pub.id
                cand.state = ItemState.superseded
                ev = Event(
                    pipeline_item_id=cand.id,
                    type=EventType.item_resolved,
                    payload={
                        "resolved_to": pub.id,
                        "final_title": pub.title,
                        "via": "published version matches consultation subject",
                    },
                )
                ev2 = Event(
                    pipeline_item_id=cand.id,
                    type=EventType.state_changed,
                    payload={"from": old_state.value, "to": ItemState.superseded.value},
                )
                db.add_all([ev, ev2])
                events.extend([ev, ev2])
                break
    db.flush()
    return events


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
