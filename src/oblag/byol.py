"""BYOL (bring-your-own-license) private store (spec 06 layer 4).

Users who legitimately own copyrighted standards drop their copies here for LOCAL
analysis. Invariant (spec 00 #3): nothing in this module writes to pipeline items,
events, snapshots, RSS, webhooks, or any shared output, and private documents are
never attested to external logs."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from oblag.config import get_settings
from oblag.db.models import DisplayPolicy, Obligation, PrivateDocument
from oblag.structure import StructureDiff, diff_structures, extract_requirements


class ByolError(Exception):
    pass


def add_document(
    session: Session,
    obligation_slug: str,
    version_label: str,
    source_path: Path,
    *,
    license_attested: bool,
    org_id: int,
) -> PrivateDocument:
    if not license_attested:
        raise ByolError(
            "BYOL requires attesting that you hold a license for this document (--attest-license)"
        )
    obligation = session.query(Obligation).filter_by(slug=obligation_slug).one_or_none()
    if obligation is None:
        raise ByolError(f"unknown obligation {obligation_slug!r} (run `oblag seed`?)")
    if not source_path.exists():
        raise ByolError(f"file not found: {source_path}")

    content = source_path.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    # org-partitioned storage: an org's files never share a directory with another's
    dest_dir = get_settings().private_dir / f"org-{org_id}" / obligation_slug / version_label
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source_path.name
    shutil.copyfile(source_path, dest)

    existing = (
        session.query(PrivateDocument)
        .filter_by(org_id=org_id, obligation_id=obligation.id, version_label=version_label)
        .one_or_none()
    )
    if existing is not None:
        existing.sha256 = sha
        existing.storage_ref = str(dest.relative_to(get_settings().private_dir))
        existing.license_attested_at = datetime.now(UTC)
        session.flush()
        return existing
    doc = PrivateDocument(
        org_id=org_id,
        obligation_id=obligation.id,
        version_label=version_label,
        sha256=sha,
        storage_ref=str(dest.relative_to(get_settings().private_dir)),
        license_attested_at=datetime.now(UTC),
    )
    session.add(doc)
    session.flush()
    return doc


def list_documents(session: Session, org_id: int) -> list[PrivateDocument]:
    """This org's BYOL documents only — never another tenant's (spec 07 §6)."""
    return (
        session.query(PrivateDocument)
        .filter_by(org_id=org_id)
        .order_by(PrivateDocument.obligation_id, PrivateDocument.version_label)
        .all()
    )


def _read_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise ByolError("PDF support requires: pip install 'oblag[pdf]'") from exc
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return path.read_text(encoding="utf-8", errors="replace")


def _doc_path(doc: PrivateDocument) -> Path:
    return get_settings().private_dir / doc.storage_ref


@dataclass
class GatedDiff:
    policy: DisplayPolicy
    counts: dict[str, int]
    added: list[dict] | None = None  # None = withheld by display_policy
    removed: list[dict] | None = None


def diff_versions(
    session: Session, obligation_slug: str, from_version: str, to_version: str, *, org_id: int
) -> GatedDiff:
    obligation = session.query(Obligation).filter_by(slug=obligation_slug).one_or_none()
    if obligation is None:
        raise ByolError(f"unknown obligation {obligation_slug!r}")
    docs: dict[str, PrivateDocument] = {}
    for version in (from_version, to_version):
        doc = (
            session.query(PrivateDocument)
            .filter_by(org_id=org_id, obligation_id=obligation.id, version_label=version)
            .one_or_none()
        )
        if doc is None:
            raise ByolError(f"no BYOL document for {obligation_slug} {version!r}")
        docs[version] = doc

    old = extract_requirements(_read_text(_doc_path(docs[from_version])))
    new = extract_requirements(_read_text(_doc_path(docs[to_version])))
    diff = diff_structures(old, new)
    return _gate(diff, obligation.display_policy)


def _gate(diff: StructureDiff, policy: DisplayPolicy) -> GatedDiff:
    counts = {"added": len(diff.added), "removed": len(diff.removed), "kept": diff.kept}
    if policy is DisplayPolicy.events_only:
        return GatedDiff(policy=policy, counts=counts)
    include_headings = policy in (DisplayPolicy.ids_and_titles, DisplayPolicy.full_text)

    def render(reqs) -> list[dict]:
        return [
            {"id": r.identifier, **({"heading": r.heading} if include_headings else {})}
            for r in reqs
        ]

    return GatedDiff(
        policy=policy, counts=counts, added=render(diff.added), removed=render(diff.removed)
    )
