from __future__ import annotations

import re
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


@register_statemap("cellar")
def cellar_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    if native_status.startswith("PROP_"):
        return ItemState.proposed
    if native_status in {t for t in ("REG", "DIR", "DEC")} or native_status.endswith(
        ("_IMPL", "_DEL")
    ):
        eif = _get(dates, DateType.entry_into_force)
        if eif is not None and eif > today:
            return ItemState.final_pending_effective
        return ItemState.effective
    return None


# OEIL "Stage reached" values → canonical states (open map: unknown → anomaly).
OEIL_STAGE_MAP: dict[str, ItemState] = {
    "preparatory phase in parliament": ItemState.proposed,
    "awaiting committee decision": ItemState.proposed,
    "awaiting parliament's position in 1st reading": ItemState.proposed,
    "awaiting parliament 1st reading / single reading / budget 1st stage": ItemState.proposed,
    "awaiting council's 1st reading position": ItemState.proposed,
    "awaiting council decision": ItemState.proposed,
    "awaiting final decision": ItemState.proposed,
    "awaiting signature of act": ItemState.final_pending_effective,
    "procedure completed, awaiting publication in official journal": (
        ItemState.final_pending_effective
    ),
    "procedure completed": ItemState.effective,
    "procedure lapsed or withdrawn": ItemState.withdrawn,
    "procedure rejected": ItemState.withdrawn,
}


@register_statemap("oeil")
def oeil_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    return OEIL_STAGE_MAP.get(native_status.strip().lower())


def _comment_window_state(dates: CurrentDateMap, today: date) -> ItemState:
    cc = _get(dates, DateType.comment_close)
    if cc is None:
        return ItemState.comment_open  # window announced, close date unknown/unparsed
    return ItemState.comment_open if cc >= today else ItemState.comment_closed


@register_statemap("edpb")
def edpb_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    if native_status == "consultation":
        return _comment_window_state(dates, today)
    if native_status == "adopted":
        return ItemState.effective
    return None


@register_statemap("esma")
def esma_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    if native_status == "consultation":
        return _comment_window_state(dates, today)
    return None


@register_statemap("eba")
def eba_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    if native_status == "consultation":
        return _comment_window_state(dates, today)
    return None


@register_statemap("cppa")
def cppa_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    if native_status == "proposed":
        return _comment_window_state(dates, today)
    if native_status == "completed":
        return ItemState.effective
    return None


@register_statemap("aicpa")
def aicpa_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    # exposure drafts have no machine-readable comment window (sitemap-only source):
    # `proposed` until a curated comment_close assertion arrives, then window logic
    if native_status != "exposure_draft":
        return None
    if _get(dates, DateType.comment_close) is not None:
        return _comment_window_state(dates, today)
    return ItemState.proposed


@register_statemap("hitrust")
def hitrust_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    return ItemState.effective if native_status in ("release", "advisory") else None


@register_statemap("cis")
def cis_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    return ItemState.effective if native_status == "release" else None


@register_statemap("nerc")
def nerc_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    """Project-page status text → state. NERC's development flow: drafting/SAR →
    comment periods (+ concurrent ballots) → board adoption → FERC filing/approval."""
    s = native_status.lower()
    if not s:
        return None
    if s == "unknown":
        # status block missing/restructured — the project IS under development
        # (that's the listing's premise); an anomaly already flagged the parse
        return ItemState.proposed
    if "board adopted" in s or "filed with ferc" in s:
        # adopted; effectiveness awaits FERC approval + the standard's effective date,
        # which the status text does not carry
        return ItemState.final_pending_effective
    if "comment" in s and ("concluded" in s or "closed" in s or "ended" in s):
        return ItemState.comment_closed
    if "comment" in s and ("open" in s or "period" in s):
        return ItemState.comment_open
    if "ballot" in s:
        return ItemState.comment_closed
    return ItemState.proposed  # drafting, SAR development, team formation, …


@register_statemap("curated")
def curated_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    """Curated milestone timelines (oblag.milestones): in force once entry-into-force
    or the earliest application date has passed; pending before that."""
    if native_status != "timeline":
        return None
    starts = [
        v
        for (dt, _label), v in dates.items()
        if dt in (DateType.entry_into_force, DateType.effective, DateType.application)
    ]
    if starts and min(starts) <= today:
        return ItemState.effective
    return ItemState.final_pending_effective


@register_statemap("have_your_say")
def have_your_say_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    # The portal never closes out initiatives whose proposals became law (observed
    # live: the 2022 CRA consultation still reports ADOPTION_WORKFLOW two years after
    # Regulation 2024/2847 entered force). A curated `adopted` assertion records the
    # outcome; once present, the consultation's lifecycle is complete. Any label
    # counts — curated adoptions are labeled with the resulting act ("Regulation (EU)
    # 2024/2847"), which the unlabeled-only _get helper would miss.
    adopted = next((v for (dt, _label), v in dates.items() if dt is DateType.adopted), None)
    if adopted is not None and adopted <= today:
        return ItemState.effective
    cc = _get(dates, DateType.comment_close)
    if cc is None:
        return ItemState.proposed
    return ItemState.comment_open if cc >= today else ItemState.comment_closed


@register_statemap("legiscan")
def legiscan_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    if native_status == "vetoed":
        return ItemState.withdrawn
    if native_status in ("enrolled", "passed"):
        eff = _get(dates, DateType.effective)
        if eff is not None and eff <= today:
            return ItemState.effective
        return ItemState.final_pending_effective
    return None


@register_statemap("pci_ssc")
def pci_ssc_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    if native_status == "publication":
        return ItemState.effective  # a new version is now in force
    if native_status != "rfc":
        return None
    cc = _get(dates, DateType.comment_close)
    if cc is None:
        return ItemState.comment_open
    return ItemState.comment_open if cc >= today else ItemState.comment_closed


@register_statemap("iso_catalog")
def iso_catalog_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    """ISO harmonized stage codes (ISO Guide 69). Open map: unknown → anomaly."""
    if not re.fullmatch(r"\d{2}\.\d{2}", native_status):
        return None
    stage, sub = native_status.split(".")
    major = int(stage)
    if major < 40:
        return ItemState.proposed
    if major == 40:
        return ItemState.comment_open if sub == "20" else ItemState.comment_closed
    if major == 50:
        return ItemState.final_pending_effective
    if major in (60, 90):
        return ItemState.effective
    if major == 95:
        return ItemState.withdrawn
    return None


@register_statemap("regulations_gov")
def regulations_gov_statemap(
    native_status: str, meta: dict[str, str], dates: CurrentDateMap, today: date
) -> ItemState | None:
    # Same date semantics as Federal Register, evaluated over MERGED current dates, so
    # enrichment records lacking dates never regress an item's state. No withdrawal
    # mapping: regs.gov `withdrawn` flags a removed *document*, not the rulemaking.
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
