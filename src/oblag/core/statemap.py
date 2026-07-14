from __future__ import annotations

from collections.abc import Callable
from datetime import date

from oblag.db.models import DateType, ItemState

# Current dates for an item, keyed by (date_type, label). Values are plain dates here;
# the reducer resolves supersession chains before calling in.
CurrentDateMap = dict[tuple[DateType, str | None], date]

StatemapFn = Callable[[str, dict[str, str], CurrentDateMap, date], ItemState | None]

_STATEMAPS: dict[str, StatemapFn] = {}


def register_statemap(source_system: str) -> Callable[[StatemapFn], StatemapFn]:
    def deco(fn: StatemapFn) -> StatemapFn:
        _STATEMAPS[source_system] = fn
        return fn

    return deco


def compute_state(
    source_system: str,
    native_status: str,
    native_meta: dict[str, str],
    dates: CurrentDateMap,
    today: date,
) -> ItemState | None:
    """Map source-native status + date context to canonical state.

    Returns None when the source/native status is unknown — the caller records an
    anomaly and keeps (or conservatively initializes) the state. Never raises.
    """
    fn = _STATEMAPS.get(source_system)
    if fn is None:
        return None
    return fn(native_status, native_meta, dates, today)


def _get(dates: CurrentDateMap, dt: DateType) -> date | None:
    return dates.get((dt, None))


@register_statemap("nist_csrc")
def nist_csrc_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    if native_status == "final":
        return ItemState.effective
    cc = _get(dates, DateType.comment_close)
    if cc is None:
        # the feed lists only drafts open for comment; "No Due Date" means open-ended
        return ItemState.comment_open
    return ItemState.comment_open if cc >= today else ItemState.comment_closed


@register_statemap("federal_register")
def federal_register_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    action = meta.get("action", "").lower()
    if "withdraw" in action:
        return ItemState.withdrawn
    if native_status == "PRORULE":
        cc = _get(dates, DateType.comment_close)
        if cc is None:
            return ItemState.proposed
        return ItemState.comment_open if cc >= today else ItemState.comment_closed
    if native_status == "RULE":
        eff = _get(dates, DateType.effective)
        if eff is not None and eff > today:
            return ItemState.final_pending_effective
        return ItemState.effective
    return None
