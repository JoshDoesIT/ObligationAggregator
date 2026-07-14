"""OSCAL-compatible export (spec: DEVELOPMENT_PLAN — OSCAL-*compatible*, not a full
set-theory crosswalk). Emits a valid OSCAL 1.1.2 catalog whose back-matter resources
are the tracked pipeline items: stable UUIDs, lifecycle state / dates / join keys as
props, source links as rlinks. Interoperates with Trestle-style tooling that consumes
catalog back-matter; control-level mapping remains a curated feature."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from oblag import __version__
from oblag.core.reducer import current_dates
from oblag.db.models import Obligation, PipelineItem

_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/JoshDoesIT/ObligationAggregator")
PROP_NS = "https://github.com/JoshDoesIT/ObligationAggregator/ns/oscal"


def _uuid(kind: str, key: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{kind}:{key}"))


def export_catalog(session: Session, obligation_slug: str | None = None) -> dict[str, Any]:
    query = session.query(PipelineItem)
    title = "ObligationAggregator tracked regulatory pipeline"
    if obligation_slug:
        obligation = session.query(Obligation).filter_by(slug=obligation_slug).one_or_none()
        if obligation is None:
            raise ValueError(f"unknown obligation {obligation_slug!r}")
        query = query.filter(PipelineItem.obligation_id == obligation.id)
        title += f" — {obligation.name}"

    resources = []
    for item in query.order_by(PipelineItem.id):
        props = [
            {"name": "state", "value": item.state.value, "ns": PROP_NS},
            {"name": "source-system", "value": item.source_system, "ns": PROP_NS},
            {"name": "jurisdiction", "value": item.jurisdiction, "ns": PROP_NS},
            {"name": "track", "value": item.track, "ns": PROP_NS},
        ]
        if item.obligation:
            props.append({"name": "obligation", "value": item.obligation.slug, "ns": PROP_NS})
        for key in item.join_keys:
            props.append(
                {
                    "name": f"join-key-{key.type.replace('_', '-')}",
                    "value": key.value,
                    "ns": PROP_NS,
                }
            )
        for kd in current_dates(session, item.id).values():
            name = f"date-{kd.date_type.value.replace('_', '-')}"
            if kd.label:
                name += f"-{kd.label}"
            props.append(
                {
                    "name": name,
                    "value": kd.value.isoformat(),
                    "class": kd.confidence.value,
                    "ns": PROP_NS,
                }
            )
        resource: dict[str, Any] = {
            "uuid": _uuid("item", f"{item.source_system}:{item.id}"),
            "title": item.title,
            "props": props,
        }
        if item.abstract:
            resource["description"] = item.abstract
        if item.url:
            resource["rlinks"] = [{"href": item.url}]
        resources.append(resource)

    return {
        "catalog": {
            "uuid": _uuid("catalog", obligation_slug or "all"),
            "metadata": {
                "title": title,
                "last-modified": datetime.now(UTC).isoformat(),
                "version": __version__,
                "oscal-version": "1.1.2",
            },
            "back-matter": {"resources": resources},
        }
    }
