from __future__ import annotations

import json
import re
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
from oblag.db.models import Confidence, DateType

FEED_URL = "https://csrc.nist.gov/CSRC/media/feeds/pubs/drafts-open-for-comment.json"

# Stage suffixes observed live (spec 02: OPEN enum — unknown suffixes are anomalies,
# never crashes). The research docs knew ipd/2pd/fpd; live data adds iwd/2prd/….
KNOWN_STAGES = {
    "iwd": "Initial Working Draft",
    "iprd": "Initial Pre-Draft Call for Comments",
    "prd": "Pre-Draft Call for Comments",
    "2prd": "Second Pre-Draft Call for Comments",
    "ipd": "Initial Public Draft",
    "2pd": "Second Public Draft",
    "3pd": "Third Public Draft",
    "fpd": "Final Public Draft",
    "final": "Final",
}
_STAGE_RE = re.compile(r"/([0-9]*[a-z]+[0-9]*)$")
_DUE_RE = re.compile(r"Comments?\s+Due:?\s*(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
_NO_DUE_RE = re.compile(r"No\s+Due\s+Date", re.IGNORECASE)
# "SP 800-53" / "IR 8320E" / "CSWP 36" style series+number extraction from title
_SERIES_RE = re.compile(r"^(SP|IR|NISTIR|FIPS|CSWP|AI|ITL)\s*([0-9]{1,4}[A-Za-z]?(?:-[0-9]+)?)")

# Map well-known NIST series to shipped obligation slugs
_OBLIGATION_MAP = {
    ("SP", "800-53"): "nist-800-53",
    ("SP", "800-171"): "nist-800-171",
}


@register
class NistCsrcAdapter(SourceAdapter):
    name = "nist_csrc"
    jurisdiction = "US-Federal"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        resp = ctx.client.get(FEED_URL)
        resp.raise_for_status()
        yield RawDocument(
            url=FEED_URL,
            content=resp.content,
            http_status=resp.status_code,
            http_headers=dict(resp.headers),
        )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        try:
            feed = json.loads(raw.content.decode("utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        for entry in feed.get("entries") or []:
            item = self._normalize_entry(entry)
            if item is not None:
                yield item

    def _normalize_entry(self, entry: dict) -> NormalizedItem | None:
        url = (entry.get("id") or "").strip()
        title = (entry.get("title") or "").strip()
        if not url or not title:
            return None
        anomalies: list[str] = []

        base_url, stage = _split_stage(url)
        if stage is None:
            anomalies.append(f"unknown draft-stage suffix on {url}; treating as draft")
            stage_key = "unknown"
            stage_name = "Unknown draft stage"
            base_url = url
        else:
            stage_key = stage
            stage_name = KNOWN_STAGES[stage]
            # feed titles concatenate the stage phrase onto the title; strip it
            if title.endswith(stage_name):
                title = title[: -len(stage_name)].rstrip(" ,;–-")

        dates: list[NormalizedDate] = []
        content = entry.get("content") or ""
        due = _DUE_RE.search(content)
        if due:
            try:
                month, day, year = (int(x) for x in due.group(1).split("/"))
                dates.append(
                    NormalizedDate(
                        DateType.comment_close, date(year, month, day), Confidence.published_firm
                    )
                )
            except ValueError:
                anomalies.append(f"unparseable comments-due date {due.group(1)!r} on {url}")
        elif not _NO_DUE_RE.search(content):
            anomalies.append(f"no comments-due information in feed content for {url}")

        published = _parse_ts(entry.get("published"))
        if published:
            dates.append(
                NormalizedDate(DateType.proposal_date, published, Confidence.published_firm)
            )

        series_match = _SERIES_RE.match(title)
        obligation_slug = None
        if series_match:
            obligation_slug = _OBLIGATION_MAP.get((series_match.group(1), series_match.group(2)))

        return NormalizedItem(
            source_system=self.name,
            external_key=("nist_pub_url", base_url),
            jurisdiction=self.jurisdiction,
            title=title,
            abstract=_strip_html(entry.get("summary") or "") or None,
            url=url,
            native_status=stage_key,
            track="proposed",
            dates=dates,
            obligation_slug=obligation_slug,
            native_meta={"stage_name": stage_name, "feed_link": entry.get("link") or ""},
            anomalies=anomalies,
        )


def _split_stage(url: str) -> tuple[str, str | None]:
    """'…/sp/800/219/r2/ipd' → ('…/sp/800/219/r2', 'ipd'). Unknown suffix → (url, None).

    The base URL is the durable identity: the same publication's ipd/2pd/final share it."""
    m = _STAGE_RE.search(url.rstrip("/"))
    if m and m.group(1) in KNOWN_STAGES:
        return url.rstrip("/")[: m.start()], m.group(1)
    return url, None


def _parse_ts(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _TAG_RE.sub("", text).strip()
