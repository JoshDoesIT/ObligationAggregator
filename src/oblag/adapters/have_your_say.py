from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime

from oblag.adapters import register
from oblag.adapters.base import (
    FetchContext,
    NormalizedDate,
    NormalizedItem,
    RawDocument,
    SourceAdapter,
)
from oblag.config import get_settings
from oblag.db.models import Confidence, DateType

SEARCH_URL = "https://ec.europa.eu/info/law/better-regulation/brpapi/searchInitiatives"
INITIATIVE_URL = "https://ec.europa.eu/info/law/better-regulation/have-your-say/initiatives/{id}"
PAGE_SIZE = 50
MAX_PAGES = 10


@register
class HaveYourSayAdapter(SourceAdapter):
    """EU 'Have Your Say' (Better Regulation portal) formal feedback periods.

    Only initiatives with an actual feedback window (open or closed) become pipeline
    items — INIT_PLANNED/DISABLED planning entries are weak signals (spec 00)."""

    name = "have_your_say"
    jurisdiction = "EU"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        topics = ctx.params.get("topics") or [
            t.strip() for t in get_settings().hys_topics.split(",") if t.strip()
        ]
        for topic in topics:
            for page in range(MAX_PAGES):
                resp = ctx.client.get(
                    SEARCH_URL,
                    params={
                        "topic": topic,
                        "size": str(PAGE_SIZE),
                        "page": str(page),
                        "language": "EN",
                    },
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                yield RawDocument(
                    url=str(resp.url),
                    content=resp.content,
                    http_status=resp.status_code,
                    http_headers=dict(resp.headers),
                    meta={"topic": topic},
                )
                try:
                    body = resp.json()
                except json.JSONDecodeError:
                    break
                page_info = body.get("initiativeResultDtoPage") or {}
                if page >= int(page_info.get("totalPages") or 1) - 1:
                    break

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        try:
            body = json.loads(raw.content)
        except json.JSONDecodeError:
            return
        content = (body.get("initiativeResultDtoPage") or {}).get("content") or []
        for initiative in content:
            item = self._normalize_initiative(initiative, raw.meta.get("topic", ""))
            if item is not None:
                yield item

    def _normalize_initiative(self, ini: dict, topic: str) -> NormalizedItem | None:
        raw_id = ini.get("id")
        title = (ini.get("shortTitle") or "").strip()
        if raw_id is None or not title:
            return None
        # Relevance gate: the DIGITAL topic spans all EU digital policy (spectrum
        # conferences, platform economics) — keep security/privacy initiatives only.
        from oblag.scope import in_scope

        if not in_scope(title, ini.get("foreseenActType")):
            return None
        ini_id = str(int(raw_id))  # API returns floats ("16413.0")

        # active feedback window: current status with the latest end date
        windows = []
        for status in ini.get("currentStatuses") or []:
            end = _parse_dt(status.get("feedbackEndDate"))
            start = _parse_dt(status.get("feedbackStartDate"))
            if end is not None:
                windows.append((end, start, status.get("frontEndStage") or ""))
        if not windows:
            return None  # planning-only entry: weak signal, out of scope
        end, start, stage = max(windows)

        dates: list[NormalizedDate] = []
        if start:
            dates.append(NormalizedDate(DateType.comment_open, start, Confidence.published_firm))
        dates.append(NormalizedDate(DateType.comment_close, end, Confidence.published_firm))

        return NormalizedItem(
            source_system=self.name,
            external_key=("hys_initiative", ini_id),
            jurisdiction=self.jurisdiction,
            title=title,
            url=INITIATIVE_URL.format(id=ini_id),
            native_status=stage,
            track="proposed",
            dates=dates,
            native_meta={
                "foreseen_act_type": ini.get("foreseenActType") or "",
                "reference": ini.get("reference") or "",
                "topic": topic,
            },
        )


def _parse_dt(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S").date()
    except ValueError:
        return None
