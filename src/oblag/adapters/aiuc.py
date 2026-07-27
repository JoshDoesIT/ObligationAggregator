"""AIUC-1 quarterly releases, read from the standard's own changelog.

AIUC-1 is unusual among the frameworks here: it revises on a fixed QUARTERLY cadence
rather than whenever the issuing body gets round to it, and it publishes the next
release date in advance. So this adapter reads two things from one page — the releases
that have happened, and the one that is scheduled.

The scheduled release is emitted as an item under the SAME key its released form will
carry, so when the date arrives the row flips from scheduled to release in place
instead of a new row appearing beside a stale one.

No feed exists (checked: /rss.xml, /feed, /atom.xml all 404, and there is no <link
rel=alternate> on the page). The changelog is server-rendered though, so the release
history is in the HTML and this needs no browser. Metadata only, never control text.
"""

from __future__ import annotations

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

CHANGELOG_URL = "https://www.aiuc-1.com/changelog"

_DATE = r"([A-Z][a-z]+ \d{1,2}, \d{4})"
_CURRENT_RE = re.compile(rf"most recent version of AIUC-1 was released on\W*{_DATE}")
_NEXT_RE = re.compile(rf"next version of AIUC-1 will be released on\W*{_DATE}")
# "Standard history" lists the superseded releases; the current one is named above it
_HISTORY_RE = re.compile(rf"Version\|\s*{_DATE}")
# every changed requirement in the current release is tagged with one of these
_CATEGORY_RE = re.compile(r"Category\|(Addition|Revision|Clarification)\|")


@register
class AiucAdapter(SourceAdapter):
    """Quarterly AIUC-1 releases, plus the next one before it lands."""

    name = "aiuc"
    jurisdiction = "Global"

    def fetch_raw(self, ctx: FetchContext) -> Iterable[RawDocument]:
        resp = ctx.client.get(CHANGELOG_URL)
        resp.raise_for_status()
        yield RawDocument(
            url=CHANGELOG_URL,
            content=resp.content,
            content_type="text/html",
            http_status=resp.status_code,
            http_headers=dict(resp.headers),
        )

    def normalize(self, raw: RawDocument) -> Iterable[NormalizedItem]:
        text = _flatten(raw.content.decode("utf-8", errors="replace"))

        current_match = _CURRENT_RE.search(text)
        current = _parse_date(current_match.group(1)) if current_match else None
        # Everything under "Standard history" is superseded. Parsing the whole page for
        # dates instead would sweep up the release dates quoted inside change notes.
        history_text = text.split("Standard history", 1)[-1] if "Standard history" in text else ""
        released = {d for d in (_parse_date(m) for m in _HISTORY_RE.findall(history_text)) if d}
        if current:
            released.add(current)

        summary = _summary(text, current_match.group(1)) if current_match else None
        counts = _CATEGORY_RE.findall(text)

        for released_on in sorted(released):
            is_current = released_on == current
            yield NormalizedItem(
                source_system=self.name,
                external_key=("aiuc_release", released_on.isoformat()),
                jurisdiction=self.jurisdiction,
                title=f"AIUC-1 {released_on.isoformat()}",
                url=CHANGELOG_URL,
                # only the current release's change notes are on the page; older ones
                # link out, so claiming a summary for them would be inventing one
                abstract=summary if is_current else None,
                native_status="release",
                track="final",
                obligation_slug="aiuc-1",
                published_at=released_on,
                dates=[NormalizedDate(DateType.effective, released_on, Confidence.published_firm)],
                native_meta={
                    "published_version": released_on.isoformat(),
                    **(
                        {
                            "additions": str(counts.count("Addition")),
                            "revisions": str(counts.count("Revision")),
                            "clarifications": str(counts.count("Clarification")),
                        }
                        if is_current and counts
                        else {}
                    ),
                },
            )

        next_match = _NEXT_RE.search(text)
        scheduled = _parse_date(next_match.group(1)) if next_match else None
        if scheduled and scheduled not in released:
            yield NormalizedItem(
                source_system=self.name,
                # the key its released form will carry, so this row becomes that row
                external_key=("aiuc_release", scheduled.isoformat()),
                jurisdiction=self.jurisdiction,
                title=f"AIUC-1 {scheduled.isoformat()} (scheduled)",
                url=CHANGELOG_URL,
                abstract="AIUC-1 revises on a fixed quarterly cadence. "
                "The issuer has announced this release date in advance.",
                native_status="scheduled",
                track="proposed",
                obligation_slug="aiuc-1",
                dates=[
                    NormalizedDate(DateType.projected_final, scheduled, Confidence.published_firm)
                ],
                native_meta={"scheduled_for": scheduled.isoformat()},
            )


def _flatten(html: str) -> str:
    """Tags to pipes. The page is a Next.js render, so the words we key on are split
    across spans; collapsing every tag to one separator makes the anchors contiguous."""
    html = re.sub(r"<(script|style).*?</\1>", "", html, flags=re.S | re.I)
    return re.sub(r"\|+", "|", re.sub(r"<[^>]+>", "|", html))


def _summary(text: str, current_label: str) -> str | None:
    """The paragraph the page runs under '<date> release'."""
    match = re.search(rf"{re.escape(current_label)} release\|([^|]{{40,}})", text)
    return match.group(1).strip() if match else None


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%B %d, %Y").date()
    except ValueError:
        return None
