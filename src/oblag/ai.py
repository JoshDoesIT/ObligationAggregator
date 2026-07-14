"""Optional AI assistance (spec: assistive drafts ONLY — off unless configured).

Design constraints from the research docs' own evidence (17-33% hallucination in
purpose-built legal RAG tools): outputs are drafts printed to the caller with mandatory
source citations (snapshot digests + URLs) and an explicit disclaimer; nothing is ever
auto-published or written back to shared tables. Supports the Anthropic API or any
OpenAI-compatible endpoint (incl. local Ollama/vLLM for privacy-preserving inference)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from oblag.config import get_settings
from oblag.core.reducer import current_dates
from oblag.db.models import Event, PipelineItem, Snapshot

DISCLAIMER = (
    "AI-ASSISTED DRAFT — not legal or compliance advice. Verify every claim against "
    "the cited source snapshots before acting."
)

_SYSTEM = (
    "You are drafting a change summary for a GRC engineer. Use ONLY the provided "
    "source data. Cite the snapshot digest for every factual claim in the form "
    "[sha256:<first 12 chars>]. If the data does not support a claim, say so. "
    "Dates marked agency_estimate or derived MUST be described as estimates."
)


class AiNotConfigured(Exception):
    pass


@dataclass
class Draft:
    text: str
    citations: list[dict]
    model: str

    def render(self) -> str:
        cites = "\n".join(
            f"  [sha256:{c['sha256'][:12]}] {c['source_url']} (fetched {c['fetched_at']})"
            for c in self.citations
        )
        return f"{DISCLAIMER}\n\n{self.text}\n\nSources:\n{cites}"


def _context_for_item(session: Session, item: PipelineItem) -> tuple[str, list[dict]]:
    dates = [
        {
            "type": kd.date_type.value,
            "label": kd.label,
            "value": kd.value.isoformat(),
            "confidence": kd.confidence.value,
            "snapshot_id": kd.source_snapshot_id,
        }
        for kd in current_dates(session, item.id).values()
    ]
    events = [
        {"type": e.type.value, "payload": e.payload, "at": str(e.occurred_at)}
        for e in session.query(Event)
        .filter_by(pipeline_item_id=item.id)
        .order_by(Event.id)
        .limit(50)
    ]
    snapshot_ids = {d["snapshot_id"] for d in dates if d["snapshot_id"]}
    citations = []
    for sid in sorted(snapshot_ids):
        snap = session.get(Snapshot, sid)
        if snap:
            citations.append(
                {
                    "sha256": snap.sha256,
                    "source_url": snap.source_url,
                    "fetched_at": snap.fetched_at.isoformat(),
                }
            )
    context = json.dumps(
        {
            "title": item.title,
            "state": item.state.value,
            "jurisdiction": item.jurisdiction,
            "abstract": item.abstract,
            "native_status": item.native_status,
            "dates": dates,
            "events": events,
            "sources": citations,
        },
        indent=1,
        default=str,
    )
    return context, citations


def summarize_item(session: Session, item_id: int) -> Draft:
    settings = get_settings()
    if not settings.ai_provider:
        raise AiNotConfigured(
            "AI assistance is off. Set OBLAG_AI_PROVIDER=anthropic|openai-compatible "
            "(plus OBLAG_AI_API_KEY / OBLAG_AI_BASE_URL) to enable."
        )
    item = session.get(PipelineItem, item_id)
    if item is None:
        raise ValueError(f"no item {item_id}")
    context, citations = _context_for_item(session, item)
    prompt = (
        "Draft a concise (<200 word) change summary of this regulatory pipeline item "
        f"for a GRC engineer, with citations:\n\n{context}"
    )
    if settings.ai_provider == "anthropic":
        text = _call_anthropic(prompt)
    else:
        text = _call_openai_compatible(prompt)
    return Draft(text=text, citations=citations, model=settings.ai_model)


def _call_anthropic(prompt: str) -> str:
    settings = get_settings()
    resp = httpx.post(
        (settings.ai_base_url or "https://api.anthropic.com") + "/v1/messages",
        headers={
            "x-api-key": settings.ai_api_key or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": settings.ai_model,
            "max_tokens": 1000,
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    return "".join(block.get("text", "") for block in resp.json().get("content", [])).strip()


def _call_openai_compatible(prompt: str) -> str:
    settings = get_settings()
    if not settings.ai_base_url:
        raise AiNotConfigured("openai-compatible provider requires OBLAG_AI_BASE_URL")
    headers = {"content-type": "application/json"}
    if settings.ai_api_key:
        headers["authorization"] = f"Bearer {settings.ai_api_key}"
    resp = httpx.post(
        settings.ai_base_url.rstrip("/") + "/chat/completions",
        headers=headers,
        json={
            "model": settings.ai_model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    return (resp.json()["choices"][0]["message"]["content"] or "").strip()
