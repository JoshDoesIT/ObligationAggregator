from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date, timedelta

from oblag.adapters import register
from oblag.adapters.base import (
    FetchContext,
    NormalizedDate,
    NormalizedItem,
    RawDocument,
    SourceAdapter,
)
from oblag.db.models import Confidence, DateType

SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"

ACT_TYPES = [
    "REG",
    "DIR",
    "DEC",
    "REG_IMPL",
    "REG_DEL",
    "DIR_IMPL",
    "DIR_DEL",
    "DEC_IMPL",
    "DEC_DEL",
]
PROPOSAL_TYPES = ["PROP_REG", "PROP_DIR", "PROP_DEC"]

_CORRIGENDUM_RE = re.compile(r"^(?P<base>.+?)R\(\d+\)$")

# Known CELEX numbers → shipped obligation slugs, so EU items auto-link to the
# obligations GRC teams filter/alert on (amendments and corrigenda land on the
# same item via the celex join key).
CELEX_OBLIGATION_MAP = {
    "32016R0679": "gdpr",
    "32022R2554": "dora",
    "32022L2555": "nis2",
    "32024R1689": "eu-ai-act",
    "32024R2847": "eu-cra",
    "32024R1183": "eidas2",
}

_PREFIX = (
    "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
    "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
)


def _acts_query(start: date, end: date) -> str:
    types = ", ".join(
        f"<http://publications.europa.eu/resource/authority/resource-type/{t}>" for t in ACT_TYPES
    )
    return (
        _PREFIX
        + f"""
SELECT ?celex ?type ?date
       (GROUP_CONCAT(DISTINCT ?force; separator=",") AS ?forces)
       (GROUP_CONCAT(DISTINCT ?deadline; separator=",") AS ?deadlines)
       (SAMPLE(?title_) AS ?title)
WHERE {{
  ?work cdm:work_has_resource-type ?type .
  ?work cdm:resource_legal_id_celex ?celex .
  ?work cdm:work_date_document ?date .
  OPTIONAL {{ ?work cdm:resource_legal_date_entry-into-force ?force }}
  OPTIONAL {{ ?work cdm:resource_legal_date_deadline ?deadline }}
  OPTIONAL {{ ?exp cdm:expression_belongs_to_work ?work .
             ?exp cdm:expression_uses_language
                  <http://publications.europa.eu/resource/authority/language/ENG> .
             ?exp cdm:expression_title ?title_ }}
  FILTER(?type IN ({types}))
  FILTER(?date >= "{start.isoformat()}"^^xsd:date && ?date <= "{end.isoformat()}"^^xsd:date)
}}
GROUP BY ?celex ?type ?date
ORDER BY DESC(?date)
"""
    )


def _proposals_query(start: date, end: date) -> str:
    types = ", ".join(
        f"<http://publications.europa.eu/resource/authority/resource-type/{t}>"
        for t in PROPOSAL_TYPES
    )
    return (
        _PREFIX
        + f"""
SELECT ?celex ?type ?date (SAMPLE(?title_) AS ?title)
WHERE {{
  ?work cdm:work_has_resource-type ?type .
  ?work cdm:resource_legal_id_celex ?celex .
  ?work cdm:work_date_document ?date .
  OPTIONAL {{ ?exp cdm:expression_belongs_to_work ?work .
             ?exp cdm:expression_uses_language
                  <http://publications.europa.eu/resource/authority/language/ENG> .
             ?exp cdm:expression_title ?title_ }}
  FILTER(?type IN ({types}))
  FILTER(?date >= "{start.isoformat()}"^^xsd:date && ?date <= "{end.isoformat()}"^^xsd:date)
}}
GROUP BY ?celex ?type ?date
ORDER BY DESC(?date)
"""
    )


@register
class CellarAdapter(SourceAdapter):
    """EUR-Lex/CELLAR: EU acts (incl. delegated/implementing) and COM proposals."""

    name = "cellar"
    jurisdiction = "EU"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        if ctx.window:
            start, end = ctx.window
        elif ctx.since:
            start, end = ctx.since.date(), date.today() + timedelta(days=1)
        else:
            start, end = date.today() - timedelta(days=7), date.today() + timedelta(days=1)
        for kind, query in (
            ("acts", _acts_query(start, end)),
            ("proposals", _proposals_query(start, end)),
        ):
            resp = ctx.client.get(
                SPARQL_ENDPOINT,
                params={"query": query, "format": "application/sparql-results+json"},
                timeout=90.0,
            )
            resp.raise_for_status()
            yield RawDocument(
                url=str(resp.url),
                content=resp.content,
                http_status=resp.status_code,
                http_headers=dict(resp.headers),
                meta={"kind": kind},
            )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        try:
            body = json.loads(raw.content)
        except json.JSONDecodeError:
            return
        for row in (body.get("results") or {}).get("bindings") or []:
            item = self._normalize_row(row, raw.meta.get("kind", "acts"))
            if item is not None:
                yield item

    def _normalize_row(self, row: dict, kind: str) -> NormalizedItem | None:
        celex = _v(row, "celex")
        doc_date = _parse_date(_v(row, "date"))
        if not celex or doc_date is None:
            return None
        rtype = (_v(row, "type") or "").rsplit("/", 1)[-1]
        title = _v(row, "title") or celex

        corr = _CORRIGENDUM_RE.match(celex)
        if corr:
            # Corrigenda are erratum signals on the base act, not items (spec 05)
            return NormalizedItem(
                source_system=self.name,
                external_key=("celex", corr.group("base")),
                jurisdiction=self.jurisdiction,
                title=title,
                url=_eurlex_url(corr.group("base")),
                native_status=rtype,
                track="final",
                obligation_slug=CELEX_OBLIGATION_MAP.get(corr.group("base")),
                anomalies=[f"corrigendum {celex} published {doc_date.isoformat()}"],
            )

        anomalies: list[str] = []
        dates: list[NormalizedDate] = []
        if kind == "proposals":
            track = "proposed"
            dates.append(
                NormalizedDate(DateType.proposal_date, doc_date, Confidence.published_firm)
            )
        else:
            track = "final"
            dates.append(NormalizedDate(DateType.adopted, doc_date, Confidence.published_firm))
            forces = _parse_date_list(_v(row, "forces"))
            if forces:
                dates.append(
                    NormalizedDate(DateType.entry_into_force, forces[0], Confidence.published_firm)
                )
                for n, extra in enumerate(forces[1:], start=1):
                    dates.append(
                        NormalizedDate(
                            DateType.phased_compliance,
                            extra,
                            Confidence.published_firm,
                            label=f"application-{n}",
                        )
                    )
            for n, deadline in enumerate(_parse_date_list(_v(row, "deadlines")), start=1):
                dates.append(
                    NormalizedDate(
                        DateType.transition_deadline,
                        deadline,
                        Confidence.published_firm,
                        label=f"deadline-{n}",
                    )
                )

        return NormalizedItem(
            source_system=self.name,
            external_key=("celex", celex),
            jurisdiction=self.jurisdiction,
            title=title,
            url=_eurlex_url(celex),
            native_status=rtype,
            track=track,
            dates=dates,
            obligation_slug=CELEX_OBLIGATION_MAP.get(celex),
            native_meta={"resource_type": rtype},
            anomalies=anomalies,
        )


def _v(row: dict, key: str) -> str | None:
    cell = row.get(key)
    return cell.get("value") if isinstance(cell, dict) else None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_date_list(value: str | None) -> list[date]:
    out = []
    for part in (value or "").split(","):
        d = _parse_date(part.strip())
        if d is not None:
            out.append(d)
    return sorted(set(out))


def _eurlex_url(celex: str) -> str:
    return f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"
