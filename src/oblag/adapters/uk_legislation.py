"""What has amended UK data-protection law, from legislation.gov.uk's changes feeds.

UK GDPR had no source at all. The text lives at legislation.gov.uk as retained EU
Regulation 2016/679, alongside the Data Protection Act 2018, and neither publishes a
newsroom. What legislation.gov.uk does publish, for every piece of legislation, is a
changes feed: `/changes/affected/eur/2016/679/data.feed`, an Atom feed whose entries
carry a structured `ukm:Effect` naming the amending instrument, the provisions it
touches, what it did to them, and when that came into force.

One row per amending instrument, not per provision. The Data (Use and Access) Act 2025
amends dozens of articles, and fifty near-identical rows saying so would bury the one
fact a reader needs, which is that the Act changed UK GDPR and here is what it touched.
Grouping also makes the identity stable: the same instrument reappears in the feed as
more of its provisions commence, and it updates its row instead of stacking new ones.

The feed states commencement per effect, so an amendment not yet in force is recorded as
pending with its date rather than announced as current law.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from defusedxml import ElementTree

from oblag.adapters import register
from oblag.adapters.base import (
    FetchContext,
    NormalizedDate,
    NormalizedItem,
    RawDocument,
    SourceAdapter,
)
from oblag.db.models import Confidence, DateType

_UKM = "{http://www.legislation.gov.uk/namespaces/metadata}"
_BASE = "https://www.legislation.gov.uk"


@dataclass(frozen=True)
class WatchedAct:
    key: str
    obligation: str
    path: str  # legislation.gov.uk id path, e.g. "eur/2016/679"
    short_name: str  # how a reader refers to it


WATCHED: tuple[WatchedAct, ...] = (
    WatchedAct(
        key="uk-gdpr",
        obligation="uk-gdpr",
        path="eur/2016/679",
        short_name="UK GDPR",
    ),
    WatchedAct(
        key="dpa-2018",
        obligation="uk-gdpr",  # the catalog tracks the pair as one obligation
        path="ukpga/2018/12",
        short_name="Data Protection Act 2018",
    ),
)


@register
class UkLegislationAdapter(SourceAdapter):
    """Amendments to the UK data-protection statutes, grouped by amending instrument."""

    name = "uk_legislation"
    jurisdiction = "UK"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        for act in WATCHED:
            url = f"{_BASE}/changes/affected/{act.path}/data.feed"
            resp = ctx.client.get(url, headers={"Accept": "application/atom+xml"})
            resp.raise_for_status()
            yield RawDocument(
                url=url,
                content=resp.content,
                content_type="application/atom+xml",
                http_status=resp.status_code,
                http_headers=dict(resp.headers),
                meta={"act": act.key},
            )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        act = next((a for a in WATCHED if a.key == raw.meta.get("act")), None)
        if act is None:
            return
        try:
            root = ElementTree.fromstring(raw.content)
        except ElementTree.ParseError:
            return
        grouped: dict[str, list[dict]] = {}
        for effect in root.iter(f"{_UKM}Effect"):
            parsed = _parse_effect(effect)
            if parsed is None:
                continue
            grouped.setdefault(parsed["affecting_uri"], []).append(parsed)
        today = datetime.now(UTC).date()
        for uri, effects in grouped.items():
            yield _item(self.name, self.jurisdiction, act, uri, effects, today)


def _parse_effect(effect) -> dict | None:
    uri = effect.get("AffectingURI")
    title = (effect.findtext(f"{_UKM}AffectingTitle") or "").strip()
    if not uri or not title:
        return None  # an effect we cannot attribute is not an effect we can report
    in_force, qualification = _in_force(effect)
    return {
        "affecting_uri": uri,
        "affecting_title": title,
        "provisions": (effect.get("AffectedProvisions") or "").strip(),
        "type": (effect.get("Type") or "").strip(),
        "in_force": in_force,
        "qualification": qualification,
        # The source distinguishes "not commenced yet" from "we have no date": a
        # prospective effect with no Date is legislation.gov.uk saying the instrument
        # commences on a day to be appointed. Recorded as such rather than guessed at.
        "prospective": _prospective(effect),
    }


def _prospective(effect) -> bool:
    if effect.get("Applied", "").lower() == "false":
        return True
    return any(n.get("Prospective", "").lower() == "true" for n in effect.iter(f"{_UKM}InForce"))


def _in_force(effect) -> tuple[date | None, str]:
    """Earliest stated commencement for this effect, and how the source qualified it."""
    best: date | None = None
    qualification = ""
    for node in effect.iter(f"{_UKM}InForce"):
        when = _date(node.get("Date"))
        if when is None:
            continue
        if best is None or when < best:
            best = when
            qualification = (node.get("Qualification") or "").strip()
    return best, qualification


def _item(
    source: str,
    jurisdiction: str,
    act: WatchedAct,
    uri: str,
    effects: list[dict],
    today: date,
) -> NormalizedItem:
    amending = effects[0]["affecting_title"]
    provisions = sorted({e["provisions"] for e in effects if e["provisions"]})
    dates = [e["in_force"] for e in effects if e["in_force"]]
    # The instrument is in force against this act once its FIRST effect commences; the
    # rest of it commencing later is why the row keeps updating.
    earliest = min(dates) if dates else None
    in_force = earliest is not None and earliest <= today
    return NormalizedItem(
        source_system=source,
        # identity is (what changed, what changed it) — stable as more provisions of the
        # same instrument commence over the years
        external_key=("uk_effect", f"{act.key}|{uri.rsplit('/id/', 1)[-1]}"),
        jurisdiction=jurisdiction,
        title=f"{act.short_name} amended by {amending}",
        abstract=_abstract(act, effects, provisions, earliest, in_force),
        url=uri.replace("http://www.legislation.gov.uk/id/", f"{_BASE}/"),
        native_status="in_force" if in_force else "pending",
        track="final",
        obligation_slug=act.obligation,
        published_at=earliest,
        dates=(
            [NormalizedDate(DateType.effective, earliest, Confidence.published_firm)]
            if earliest
            else []
        ),
        native_meta={
            "affecting": amending,
            "provisions": "; ".join(provisions)[:2000],
            "effects": str(len(effects)),
        },
    )


def _abstract(
    act: WatchedAct,
    effects: list[dict],
    provisions: list[str],
    earliest: date | None,
    in_force: bool,
) -> str:
    kinds = sorted({e["type"].lower() for e in effects if e["type"]})
    parts = [f"{len(effects)} change{'s' if len(effects) != 1 else ''} to {act.short_name}"]
    if kinds:
        parts.append(f"({', '.join(kinds)})")
    if provisions:
        shown = "; ".join(provisions[:12])
        if len(provisions) > 12:
            shown += f"; and {len(provisions) - 12} more"
        parts.append(f"affecting {shown}.")
    else:
        parts[-1] += "."
    if earliest:
        parts.append(
            f"{'In force since' if in_force else 'Comes into force'} {earliest.isoformat()}."
        )
    elif all(e["prospective"] for e in effects):
        parts.append(
            "Not yet in force: the source records these as prospective, commencing on a "
            "day to be appointed, so no compliance date can be stated."
        )
    else:
        parts.append("No commencement date stated yet.")
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None
