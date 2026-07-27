"""When the rule text itself last changed, from the eCFR versioner API.

Some obligations are a piece of the Code of Federal Regulations and nothing else. The
GLBA Safeguards Rule is 16 CFR 314; the SEC cybersecurity disclosure requirement is
Item 106 of Regulation S-K (17 CFR 229.106); SOX internal-control obligations are
17 CFR 240.13a-15 and 240.15d-15. All three showed nothing at all, because the only
adapter that could have seen them is the Federal Register, and a rulemaking scrolls out
of the Federal Register's window in a couple of years while the rule stays in force.
229.106 was adopted in August 2023 and had already fallen out of view.

eCFR publishes a versioner API that answers exactly the right question: for a part, when
was each section last amended. `/api/versioner/v1/versions/title-17.json?part=229` gives
one row per section per amendment, with the amendment date. The most recent amendment
across a watched target IS the state of that obligation, and when a new one lands the row
updates — the same idea as standard_pages, against an API rather than a page.

Narrow on purpose. A watched target names the exact part (and, where a part is enormous
and only one section is the obligation, the exact sections). Regulation S-K is 200+
sections about executive pay and mine safety; only 229.106 is a cybersecurity rule, and
watching the whole part would report every unrelated SEC amendment as a change to it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from oblag.adapters import register
from oblag.adapters.base import (
    FetchContext,
    NormalizedDate,
    NormalizedItem,
    RawDocument,
    SourceAdapter,
)
from oblag.db.models import Confidence, DateType

API = "https://www.ecfr.gov/api/versioner/v1/versions"


@dataclass(frozen=True)
class WatchedRule:
    key: str  # identity of the row this target maintains
    obligation: str
    cfr_title: str  # CFR title number, as eCFR spells it
    part: str
    label: str  # how a reader refers to it
    # Empty means the whole part is the obligation. Naming sections narrows a part whose
    # other sections are about something else entirely (see the Reg S-K note above).
    sections: tuple[str, ...] = ()

    @property
    def citation(self) -> str:
        if len(self.sections) == 1:
            return f"{self.cfr_title} CFR {self.sections[0]}"
        return f"{self.cfr_title} CFR {self.part}"

    @property
    def url(self) -> str:
        return f"https://www.ecfr.gov/current/title-{self.cfr_title}/part-{self.part}"


WATCHED: tuple[WatchedRule, ...] = (
    WatchedRule(
        key="16-314",
        obligation="glba-safeguards",
        cfr_title="16",
        part="314",
        label="GLBA Safeguards Rule",
    ),
    WatchedRule(
        key="17-229-106",
        obligation="sec-cyber-disclosure",
        cfr_title="17",
        part="229",
        sections=("229.106",),
        label="Regulation S-K Item 106, cybersecurity",
    ),
    WatchedRule(
        key="17-240-icfr",
        obligation="sox",
        cfr_title="17",
        part="240",
        sections=("240.13a-15", "240.15d-15"),
        label="Exchange Act rules 13a-15 and 15d-15, disclosure controls and ICFR",
    ),
    WatchedRule(
        key="17-229-308",
        obligation="sox",
        cfr_title="17",
        part="229",
        sections=("229.308",),
        label="Regulation S-K Item 308, internal control report",
    ),
)


@register
class EcfrAdapter(SourceAdapter):
    """Amendment dates for the CFR parts that ARE an obligation."""

    name = "ecfr"
    jurisdiction = "US-Federal"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        for rule in WATCHED:
            url = f"{API}/title-{rule.cfr_title}.json?part={rule.part}"
            resp = ctx.client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            yield RawDocument(
                url=url,
                content=resp.content,
                content_type="application/json",
                http_status=resp.status_code,
                http_headers=dict(resp.headers),
                meta={"rule": rule.key},
            )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        import json

        rule = next((r for r in WATCHED if r.key == raw.meta.get("rule")), None)
        if rule is None:
            return
        try:
            versions = json.loads(raw.content)["content_versions"]
        except (ValueError, KeyError, TypeError):
            return  # a shape we don't recognise yields nothing rather than a guess
        if not isinstance(versions, list):
            return  # ...including one where the key is present but is not a list
        rows = [v for v in versions if isinstance(v, dict) and _wanted(v, rule)]
        # A removed section is a real amendment (the FTC deleting a requirement is a
        # change), so removals count toward the date; they just don't get listed as
        # current text.
        dated: list[tuple[date, dict]] = []
        for v in rows:
            when = _date(v.get("amendment_date"))
            if when is not None:
                dated.append((when, v))
        if not dated:
            return
        latest = max(d for d, _v in dated)
        changed = sorted({v["identifier"] for d, v in dated if d == latest})
        yield NormalizedItem(
            source_system=self.name,
            # one row per watched target: a new amendment updates it in place, which is
            # the change we are here to catch
            external_key=("cfr_target", rule.key),
            jurisdiction=self.jurisdiction,
            title=f"{rule.citation} ({rule.label}): last amended {latest.isoformat()}",
            url=rule.url,
            native_status="in_force",
            track="final",
            obligation_slug=rule.obligation,
            published_at=latest,
            dates=[NormalizedDate(DateType.effective, latest, Confidence.published_firm)],
            native_meta={
                "citation": rule.citation,
                "last_amended": latest.isoformat(),
                "sections_amended": ", ".join(changed),
            },
        )


def _wanted(version: dict, rule: WatchedRule) -> bool:
    if rule.sections:
        return version.get("identifier") in rule.sections
    return version.get("part") == rule.part


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
