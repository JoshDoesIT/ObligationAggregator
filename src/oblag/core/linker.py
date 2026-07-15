from __future__ import annotations

from sqlalchemy.orm import Session, aliased

from oblag.db.models import Event, EventType, ItemState, JoinKey, PipelineItem

# Join-key types strong enough to assert proposed→final lineage (spec 03).
LINK_KEY_TYPES = ("rin", "celex", "oeil_procedure", "bill_id")


def link_resolved_items(session: Session) -> list[Event]:
    """Resolve proposed-track items to their final-track successor via shared join keys.

    The proposed item gets resolved_change_id set, transitions to `superseded`, and emits
    `item_resolved`. The final item remains the live item carrying effective dates."""
    events: list[Event] = []
    jk_prop = aliased(JoinKey)
    jk_final = aliased(JoinKey)
    prop_item = aliased(PipelineItem)
    final_item = aliased(PipelineItem)

    pairs = (
        session.query(prop_item, final_item)
        .join(jk_prop, jk_prop.pipeline_item_id == prop_item.id)
        .join(
            jk_final,
            (jk_final.type == jk_prop.type) & (jk_final.value == jk_prop.value),
        )
        .join(final_item, jk_final.pipeline_item_id == final_item.id)
        .filter(
            prop_item.track == "proposed",
            final_item.track == "final",
            prop_item.resolved_change_id.is_(None),
            jk_prop.type.in_(LINK_KEY_TYPES),
            prop_item.state.notin_([ItemState.withdrawn]),
        )
        .distinct()
        .all()
    )
    for proposed, final in pairs:
        proposed.resolved_change_id = final.id
        old_state = proposed.state
        proposed.state = ItemState.superseded
        ev = Event(
            pipeline_item_id=proposed.id,
            type=EventType.item_resolved,
            payload={
                "resolved_to": final.id,
                "final_title": final.title,
                "via": "shared join key",
            },
        )
        session.add(ev)
        events.append(ev)
        ev2 = Event(
            pipeline_item_id=proposed.id,
            type=EventType.state_changed,
            payload={"from": old_state.value, "to": ItemState.superseded.value},
        )
        session.add(ev2)
        events.append(ev2)
    session.flush()
    return events
