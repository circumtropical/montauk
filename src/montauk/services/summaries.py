"""On-demand purpose-specific summaries (spec 24.2).

A generated summary is a **cache entry**, not an authoritative fact. It is
built from curated memory only (transcript excerpts arrive with the
connector increment). When no LLM is configured, or a budget/limit blocks
it, the result is returned with ``generated=false`` and the deterministic
evidence packet instead (spec 16.2).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import mapping
from ..db import models as orm
from ..db.crypto import SecretBox
from ..db.repositories import PeopleRepository, WorkspaceScope
from ..exporters.markdown import person_to_markdown
from ..llm.base import LLMBudgetExceeded, LLMError, LLMNotConfigured
from ..llm.factory import build_provider
from ..models import Person as DomainPerson
from . import llm_usage, model_config

DETAIL_LEVELS = ("brief", "standard", "comprehensive")

SCHEMA_VERSION = "v1"

_SYSTEM = """\
You write a short, purpose-specific summary of ONE person, for the owner of a \
private relationship-memory system.

Rules:
- Use ONLY the evidence provided. Never invent names, dates, quotes, employers, \
or facts. If the evidence does not address the purpose, say so plainly.
- Do NOT produce tasks, reminders, to-dos, commitments, action items, follow-ups, \
predictions, diagnoses, or psychological judgements.
- Preserve uncertainty: if a fact is marked medium/low confidence or is vague, \
keep it vague.
- 2-5 short paragraphs, or tight bullet points for a briefing. No preamble, no \
"Here is a summary". Plain text.
"""


@dataclass
class SummaryResult:
    person_public_id: str
    person_name: str
    purpose: str
    detail_level: str
    generated: bool
    status: str  # ok | llm_unavailable | llm_error | budget_exceeded
    body: str
    model: str | None
    provider_type: str | None
    evidence: dict[str, Any]
    cached: bool
    stale: bool
    created_at: dt.datetime | None
    note: str | None = None
    revision_refs: list[str] = field(default_factory=list)

    # Debug/inspection fields (spec 18: processing runs record model/prompt
    # version, counts, usage). Populated on a fresh generation.
    system_prompt: str | None = None
    user_prompt: str | None = None
    evidence_text: str | None = None
    evidence_item_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    reported_cost_usd: float | None = None
    latency_ms: int | None = None
    stop_reason: str | None = None


def cache_key(purpose: str, detail_level: str) -> str:
    norm = " ".join(purpose.split()).casefold()
    return hashlib.sha256(f"{detail_level}\x1f{norm}".encode()).hexdigest()


def memory_fingerprint(person_row: orm.Person) -> str:
    domain = mapping.person_to_domain(person_row)
    return hashlib.sha256(person_to_markdown(domain).encode("utf-8")).hexdigest()


def _evidence_from_person(p: DomainPerson) -> dict[str, Any]:
    core: dict[str, Any] = {}
    for f in ("birthday", "location", "company", "job_title"):
        v = getattr(p, f)
        if v is not None:
            core[f] = v.to_string() if f == "birthday" else v
    if p.desired_contact_cadence_days:
        core["desired_contact_cadence_days"] = p.desired_contact_cadence_days
    if p.aliases:
        core["aliases"] = list(p.aliases)
    facts = [f for f in p.facts if not f.related_person_id]
    relationships = [f for f in p.facts if f.related_person_id]
    return {
        "person": {"id": p.id, "name": p.name},
        "core_fields": core,
        "summary": {"id": "summary", "text": p.summary} if p.summary else None,
        "facts": [
            {
                "id": f.id,
                "text": f.text,
                "section": f.category,
                **({"date": f.date.to_string()} if f.date else {}),
                **({"confidence": f.confidence.value} if f.confidence.value != "high" else {}),
            }
            for f in facts
        ],
        "relationships": [
            {
                "id": f.id,
                "text": f.text,
                "section": f.category,
                "related_person_id": f.related_person_id,
            }
            for f in relationships
        ],
        "interactions": [
            {
                "id": i.id,
                "text": i.summary or "(interaction, no summary)",
                "date": i.date.to_string(),
                **({"channel": i.channel} if i.channel else {}),
            }
            for i in sorted(p.interactions, key=lambda i: i.date.latest(), reverse=True)
        ],
    }


def _render_evidence(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    core = payload.get("core_fields") or {}
    if core:
        lines.append("Structured fields: " + ", ".join(f"{k}={v}" for k, v in core.items()))
    if payload.get("summary"):
        lines.append(f"Profile summary: {payload['summary']['text']}")
    for label, key in (("Facts", "facts"), ("Relationships", "relationships")):
        items = payload.get(key) or []
        if items:
            lines.append(f"\n{label}:")
            for it in items:
                meta = " ".join(
                    f"[{k}={it[k]}]"
                    for k in ("section", "date", "confidence", "related_person_id")
                    if it.get(k)
                )
                lines.append(f"- {it['text']}{(' ' + meta) if meta else ''}")
    interactions = payload.get("interactions") or []
    if interactions:
        lines.append("\nInteractions:")
        for it in interactions:
            when = f" ({it['date']})" if it.get("date") else ""
            lines.append(f"- {it['text']}{when}")
    return "\n".join(lines) if lines else "(no matching evidence in this person's record)"


def _deterministic_body(name: str, purpose: str, payload: dict[str, Any]) -> str:
    return (
        f"No summarization model is configured, so this is the deterministic evidence "
        f'selected from {name}\'s record for the purpose:\n\n"{purpose}"\n\n' + _render_evidence(payload)
    )


async def generate_summary(
    session: Session,
    scope: WorkspaceScope,
    *,
    public_id: str,
    purpose: str,
    detail_level: str = "standard",
    secret_box: SecretBox | None,
    force: bool = False,
) -> SummaryResult:
    if not purpose.strip():
        raise ValueError("purpose must not be blank")
    if detail_level not in DETAIL_LEVELS:
        raise ValueError(f"detail_level must be one of {DETAIL_LEVELS}")

    repo = PeopleRepository(scope)
    row = repo.require(public_id, include_archived=True)
    domain = mapping.person_to_domain(row)

    # A person's whole curated record is small and entirely relevant to a
    # summary of that person, so the evidence packet is the full record --
    # not the narrow per-question retrieval that prepare_person_context does.
    payload = _evidence_from_person(domain)
    fingerprint = memory_fingerprint(row)
    key = cache_key(purpose, detail_level)

    existing = session.execute(
        select(orm.SummaryCacheEntry)
        .where(orm.SummaryCacheEntry.workspace_id == scope.workspace_id)
        .where(orm.SummaryCacheEntry.person_id == row.id)
        .where(orm.SummaryCacheEntry.cache_key == key)
    ).scalar_one_or_none()

    if existing is not None and not force and existing.invalidated_at is None:
        stale = existing.memory_fingerprint != fingerprint
        return SummaryResult(
            person_public_id=public_id,
            person_name=domain.name,
            purpose=purpose,
            detail_level=detail_level,
            generated=existing.generated,
            status="ok" if existing.generated else "llm_unavailable",
            body=existing.body,
            model=existing.model,
            provider_type=existing.provider_type,
            evidence=payload,
            cached=True,
            stale=stale,
            created_at=existing.created_at,
            note="This person's record changed since this summary was generated." if stale else None,
        )

    settings = session.get(orm.WorkspaceSettings, scope.workspace_id)
    resolved = model_config.resolve(session, scope.workspace_id, "summarization", secret_box=secret_box)
    provider = build_provider(resolved)

    def _persist(
        *, generated: bool, body: str, model: str | None, provider_type: str | None
    ) -> orm.SummaryCacheEntry:
        nonlocal existing
        if existing is None:
            existing = orm.SummaryCacheEntry(workspace_id=scope.workspace_id, person_id=row.id, cache_key=key)
            session.add(existing)
        existing.purpose = purpose
        existing.detail_level = detail_level
        existing.generated = generated
        existing.body = body
        existing.model = model
        existing.provider_type = provider_type
        existing.schema_version = SCHEMA_VERSION
        existing.evidence_refs = {
            "facts": [f["id"] for f in payload.get("facts", [])],
            "relationships": [f["id"] for f in payload.get("relationships", [])],
            "interactions": [i["id"] for i in payload.get("interactions", [])],
        }
        existing.memory_fingerprint = fingerprint
        existing.created_at = dt.datetime.now(dt.UTC)
        existing.invalidated_at = None
        session.flush()
        return existing

    def _fallback(status: str, note: str | None) -> SummaryResult:
        body = _deterministic_body(domain.name, purpose, payload)
        _persist(generated=False, body=body, model=None, provider_type=None)
        return SummaryResult(
            public_id,
            domain.name,
            purpose,
            detail_level,
            False,
            status,
            body,
            None,
            None,
            payload,
            cached=False,
            stale=False,
            created_at=dt.datetime.now(dt.UTC),
            note=note,
        )

    if provider is None:
        return _fallback(
            "llm_unavailable", "No summarization model is configured (Settings → Model & provider)."
        )

    try:
        llm_usage.ensure_within_budget(session, scope.workspace_id, settings)  # type: ignore[arg-type]
    except LLMBudgetExceeded as exc:
        return _fallback("budget_exceeded", str(exc))

    evidence_text = _render_evidence(payload)
    item_count = (
        len(payload.get("facts", []))
        + len(payload.get("relationships", []))
        + len(payload.get("interactions", []))
    )
    prompt = (
        f"PURPOSE: {purpose}\n"
        f"DETAIL LEVEL: {detail_level}\n\n"
        f"EVIDENCE (the whole curated record for {domain.name}):\n{evidence_text}"
    )

    def _with_debug(r: SummaryResult) -> SummaryResult:
        r.system_prompt = _SYSTEM
        r.user_prompt = prompt
        r.evidence_text = evidence_text
        r.evidence_item_count = item_count
        return r

    started = dt.datetime.now(dt.UTC)
    try:
        result = await provider.generate(system=_SYSTEM, prompt=prompt, max_output_tokens=1200)
    except LLMNotConfigured as exc:
        return _with_debug(_fallback("llm_unavailable", str(exc)))
    except LLMError as exc:
        llm_usage.record(session, scope.workspace_id, purpose="summarization", error=exc, run_ref=public_id)
        return _with_debug(
            _fallback("llm_error", f"The model call failed ({exc.category}). Showing deterministic evidence.")
        )
    latency_ms = int((dt.datetime.now(dt.UTC) - started).total_seconds() * 1000)

    usage_event = llm_usage.record(
        session,
        scope.workspace_id,
        purpose="summarization",
        result=result,
        price_override=resolved.price_override if resolved else None,
        run_ref=public_id,
    )
    _persist(
        generated=True,
        body=result.text,
        model=result.model,
        provider_type=result.provider_type,
    )
    out = SummaryResult(
        public_id,
        domain.name,
        purpose,
        detail_level,
        True,
        "ok",
        result.text,
        result.model,
        result.provider_type,
        payload,
        cached=False,
        stale=False,
        created_at=dt.datetime.now(dt.UTC),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost_usd=(
            float(usage_event.estimated_cost_usd) if usage_event.estimated_cost_usd is not None else None
        ),
        reported_cost_usd=result.reported_cost_usd,
        latency_ms=latency_ms,
        stop_reason=result.stop_reason,
    )
    return _with_debug(out)


def invalidate_for_person(session: Session, workspace_id: uuid.UUID, person_id: uuid.UUID) -> int:
    rows = (
        session.execute(
            select(orm.SummaryCacheEntry)
            .where(orm.SummaryCacheEntry.workspace_id == workspace_id)
            .where(orm.SummaryCacheEntry.person_id == person_id)
            .where(orm.SummaryCacheEntry.invalidated_at.is_(None))
        )
        .scalars()
        .all()
    )
    now = dt.datetime.now(dt.UTC)
    for r in rows:
        r.invalidated_at = now
    return len(rows)
