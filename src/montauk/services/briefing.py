"""Progressive-disclosure retrieval for agents (spec 16.2, 24.1, 24.2).

The normal agent interaction for a broad/contextual request ("brief me
before I call Mike") is:

1. ``prepare_briefing`` retrieves the relevant curated evidence, compresses
   it with the configured low-cost LLM into a factual, purpose-specific
   summary, and returns **just the summary plus lightweight metadata**
   (source refs, generated flag, coverage). It does not also dump the full
   evidence -- the point is to save the calling agent's tokens/latency.
2. The agent drills down with ``get_context_sources`` only when it needs
   the underlying records.

A narrow deterministic question ("what is Mike's birthday?") bypasses the
LLM and returns the structured value directly.

Modes:
  * ``summary_only``          -- default; briefing text + source refs.
  * ``evidence_only``         -- selected records, no prose, no provider call.
  * ``summary_with_evidence`` -- both; for debugging / auditing / high stakes.

If no LLM is configured or the call fails: ``generated=false`` + a compact
deterministic evidence packet. Never a silent fallback to another provider.

A generated briefing is temporary derived output -- it never mutates facts,
approves proposals, changes authority, or adds unsupported information. It
is cached (with its source refs + model/prompt version + a fingerprint of
the source records) and invalidated when those records change.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import RetrievalConfig
from ..db import mapping
from ..db import models as orm
from ..db.crypto import SecretBox
from ..db.repositories import PeopleRepository, WorkspaceScope
from ..llm.base import LLMBudgetExceeded, LLMError, LLMNotConfigured
from ..llm.factory import build_provider
from ..models import Person as DomainPerson
from ..person_context import build_person_context
from . import llm_usage, model_config
from .summaries import cache_key, memory_fingerprint

PROMPT_VERSION = "briefing.v1"
MODES = ("summary_only", "evidence_only", "summary_with_evidence")
COVERAGE_LEVELS = ("brief", "standard", "comprehensive")

_SYSTEM = """\
You compress a relationship-memory record about ONE person into a short,
factual, purpose-specific briefing for the owner's assistant.

Rules:
- Use ONLY the evidence provided. Never invent names, dates, quotes, employers,
  numbers, or facts. If the evidence does not cover the purpose, say so briefly.
- No tasks, reminders, to-dos, commitments, action items, follow-ups,
  predictions, diagnoses, or psychological judgements.
- Preserve uncertainty: keep vague or low-confidence facts vague.
- Be concise -- a few sentences for 'brief', at most a short paragraph or two
  for 'standard', a tight structured rundown for 'comprehensive'. No preamble.
- On the FINAL line, write exactly: SOURCE_REFS: <comma-separated fact-N / int-N
  ids you actually drew on>. Use only ids that appear in the evidence.
"""

_SOURCE_LINE_RE = re.compile(r"(?im)^\s*SOURCE_REFS:\s*(.*)$")
_REF_RE = re.compile(r"(?:fact|int)-\d+")

# --- narrow structured-field questions --------------------------------

_BRIEFING_WORDS = re.compile(
    r"\b(brief|briefing|summar|overview|rundown|catch\s?up|context|prep|prepare|"
    r"tell me about|fill me in|everything|what.s (?:new|the story|going on)|state of)\b",
    re.IGNORECASE,
)
_FIELD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("birthday", re.compile(r"\b(birthday|birth ?date|born|date of birth|d\.?o\.?b)\b", re.I)),
    ("company", re.compile(r"\b(company|employer|works? (?:at|for)|workplace)\b", re.I)),
    (
        "job_title",
        re.compile(
            r"\b(job title|role|position|what.s? (?:his|her|their) job|what do(?:es)? .* do for (?:work|a living))\b",
            re.I,
        ),
    ),
    ("location", re.compile(r"\b(where (?:do(?:es)?|is) .* live|location|city|hometown|based)\b", re.I)),
    ("email", re.compile(r"\b(e-?mail(?: address)?)\b", re.I)),
    (
        "phone",
        re.compile(
            r"\b(phone(?: number)?|cell(?: number)?|mobile(?: number)?|number to (?:call|reach|text))\b", re.I
        ),
    ),
    (
        "address",
        re.compile(
            r"\b(mailing address|home address|street address|physical address|where do(?:es)? .* live)\b",
            re.I,
        ),
    ),
    (
        "desired_contact_cadence_days",
        re.compile(r"\b(how often|contact cadence|keep in touch (?:cadence|frequency)|cadence)\b", re.I),
    ),
    ("aliases", re.compile(r"\b(nickname|goes by|also known as|other names?|aliases)\b", re.I)),
]


@dataclass
class BriefingResult:
    person_public_id: str
    person_name: str
    purpose: str
    mode: str
    coverage: str  # brief | standard | comprehensive | direct
    generated: bool
    status: str  # ok | direct | llm_unavailable | llm_error | budget_exceeded
    source_refs: list[str]
    briefing: str | None = None
    direct_field: str | None = None
    direct_value: str | None = None
    evidence: dict[str, Any] | None = None
    cached: bool = False
    stale: bool = False
    model: str | None = None
    provider_type: str | None = None
    prompt_version: str = PROMPT_VERSION
    note: str | None = None
    # internal audit / debug -- not part of agent_payload()
    evidence_snapshot: str | None = None
    system_prompt: str | None = None
    user_prompt: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    reported_cost_usd: float | None = None
    latency_ms: int | None = None
    stop_reason: str | None = None
    created_at: dt.datetime | None = None

    # --- dashboard-template compatibility (the person page renders either a
    #     SummaryResult or a BriefingResult through the same partial) ----
    @property
    def body(self) -> str:
        if self.status == "direct":
            return self.direct_value or f"(no {self.direct_field} recorded for this person)"
        return self.briefing or ""

    @property
    def detail_level(self) -> str:
        return self.coverage

    @property
    def evidence_text(self) -> str | None:
        return self.evidence_snapshot

    @property
    def evidence_item_count(self) -> int:
        return len(self.source_refs)

    def agent_payload(self) -> dict[str, Any]:
        """The lightweight response an agent receives (spec: 'only lightweight
        metadata such as the IDs of the source records and whether the summary
        was generated successfully')."""
        base: dict[str, Any] = {
            "person_id": self.person_public_id,
            "generated": self.generated,
            "coverage": self.coverage,
            "source_refs": self.source_refs,
        }
        if self.status == "direct":
            base["answer"] = self.direct_value
            base["field"] = self.direct_field
            return base
        base["mode"] = self.mode
        if self.briefing is not None:
            base["briefing"] = self.briefing
        if self.stale:
            base["stale"] = True
        if self.note:
            base["note"] = self.note
        if self.status not in ("ok", "direct"):
            base["status"] = self.status
        if self.evidence is not None:  # evidence_only / summary_with_evidence
            base["evidence"] = self.evidence
        return base


# --- retrieval -------------------------------------------------------


def _core_fields(p: DomainPerson) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if p.birthday:
        out["birthday"] = p.birthday.to_string()
    for f in ("location", "company", "job_title"):
        if getattr(p, f):
            out[f] = getattr(p, f)
    if p.desired_contact_cadence_days:
        out["desired_contact_cadence_days"] = p.desired_contact_cadence_days
    if p.aliases:
        out["aliases"] = list(p.aliases)
    if p.contact.emails:
        out["emails"] = list(p.contact.emails)
    if p.contact.phones:
        out["phones"] = list(p.contact.phones)
    if p.contact.address:
        out["address"] = p.contact.address
    return out


def _select_evidence(
    domain: DomainPerson, purpose: str, coverage: str, rcfg: RetrievalConfig
) -> tuple[dict[str, Any], list[str]]:
    """Rank + trim the curated record for `purpose` (spec 24.1). Falls back to
    the whole record when a vague purpose yields no lexical hits."""
    budget = rcfg.budget_for(coverage)
    ctx = build_person_context(
        domain,
        purpose,
        detail_level=coverage,
        budget_tokens=budget,
        semantic_index=None,
        lexical_enabled=rcfg.lexical_enabled,
    )
    payload = ctx.to_payload()
    if not payload["facts"] and not payload["interactions"] and coverage != "comprehensive":
        ctx = build_person_context(
            domain,
            purpose,
            detail_level="comprehensive",
            budget_tokens=budget,
            semantic_index=None,
            lexical_enabled=rcfg.lexical_enabled,
        )
        payload = ctx.to_payload()

    evidence = {
        "person": {"id": domain.id, "name": domain.name},
        "core_fields": _core_fields(domain),
        "summary": payload.get("summary"),
        "facts": payload.get("facts", []),
        "relationships": payload.get("relationships", []),
        "interactions": payload.get("interactions", []),
        "transcript_excerpts": [],  # arrive with the connector increment
    }
    refs = [f["id"] for f in evidence["facts"]]
    refs += [f["id"] for f in evidence["relationships"]]
    refs += [i["id"] for i in evidence["interactions"]]
    return evidence, refs


def _render_evidence(ev: dict[str, Any]) -> str:
    lines: list[str] = []
    if ev.get("core_fields"):
        lines.append("Fields: " + ", ".join(f"{k}={v}" for k, v in ev["core_fields"].items()))
    if ev.get("summary"):
        lines.append(f"Profile summary: {ev['summary']['text']}")
    for label, key in (("Facts", "facts"), ("Relationships", "relationships")):
        items = ev.get(key) or []
        if items:
            lines.append(f"\n{label}:")
            for it in items:
                meta = " ".join(
                    f"[{k}={it[k]}]"
                    for k in ("section", "date", "confidence", "related_person_id")
                    if it.get(k)
                )
                lines.append(f"- ({it['id']}) {it['text']}{(' ' + meta) if meta else ''}")
    if ev.get("interactions"):
        lines.append("\nInteractions:")
        for it in ev["interactions"]:
            when = f" ({it['date']})" if it.get("date") else ""
            lines.append(f"- ({it['id']}) {it['text']}{when}")
    return "\n".join(lines) if lines else "(this person's record has no facts or interactions yet)"


# --- narrow-question routing -----------------------------------------


def classify_narrow(purpose: str) -> str | None:
    p = purpose.strip()
    if _BRIEFING_WORDS.search(p) or len(p.split()) > 14:
        return None
    is_question = p.endswith("?") or re.match(
        r"(?i)^\s*(what|whats|what's|when|where|which|who|how|does|do|is|are)\b", p
    )
    if not is_question:
        return None
    for field_name, pat in _FIELD_PATTERNS:
        if pat.search(p):
            return field_name
    return None


def _direct_value(domain: DomainPerson, field_name: str) -> str | None:
    if field_name == "birthday":
        return domain.birthday.to_string() if domain.birthday else None
    if field_name == "aliases":
        return ", ".join(domain.aliases) or None
    if field_name == "email":
        return ", ".join(domain.contact.emails) or None
    if field_name == "phone":
        return ", ".join(domain.contact.phones) or None
    if field_name == "address":
        return domain.contact.address
    if field_name == "desired_contact_cadence_days":
        v = domain.desired_contact_cadence_days
        return f"every {v} days" if v else None
    return getattr(domain, field_name, None)


# --- the operation --------------------------------------------------


async def prepare_briefing(
    session: Session,
    scope: WorkspaceScope,
    *,
    public_id: str,
    purpose: str,
    detail_level: str = "standard",
    mode: str = "summary_only",
    max_tokens: int | None = None,
    secret_box: SecretBox | None,
    force: bool = False,
    retrieval: RetrievalConfig | None = None,
) -> BriefingResult:
    if not purpose.strip():
        raise ValueError("purpose must not be blank")
    if detail_level not in COVERAGE_LEVELS:
        raise ValueError(f"detail_level must be one of {COVERAGE_LEVELS}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")

    repo = PeopleRepository(scope)
    row = repo.require(public_id, include_archived=True)
    domain = mapping.person_to_domain(row)
    rcfg = retrieval or RetrievalConfig()

    # 1. Narrow deterministic question -> structured value, no LLM.
    if mode == "summary_only":
        field_name = classify_narrow(purpose)
        if field_name is not None:
            value = _direct_value(domain, field_name)
            return BriefingResult(
                person_public_id=public_id,
                person_name=domain.name,
                purpose=purpose,
                mode="direct",
                coverage="direct",
                generated=False,
                status="direct",
                source_refs=[f"field:{field_name}"],
                direct_field=field_name,
                direct_value=value,
                note=None if value is not None else f"{field_name} is not recorded for this person",
            )

    evidence, refs = _select_evidence(domain, purpose, detail_level, rcfg)
    evidence_text = _render_evidence(evidence)
    fingerprint = memory_fingerprint(row)

    def _result(**kw: Any) -> BriefingResult:
        base = dict(
            person_public_id=public_id,
            person_name=domain.name,
            purpose=purpose,
            mode=mode,
            coverage=detail_level,
            source_refs=refs,
            created_at=dt.datetime.now(dt.UTC),
        )
        base.update(kw)
        return BriefingResult(**base)  # type: ignore[arg-type]

    # 2. evidence_only -> selected records, no provider call, no cache write.
    if mode == "evidence_only":
        return _result(generated=False, status="ok", evidence=evidence, evidence_snapshot=evidence_text)

    # 3. summary_only / summary_with_evidence -> generate (or fall back).
    key = cache_key(f"{purpose}\x1f{mode}", detail_level)
    existing = session.execute(
        select(orm.SummaryCacheEntry)
        .where(orm.SummaryCacheEntry.workspace_id == scope.workspace_id)
        .where(orm.SummaryCacheEntry.person_id == row.id)
        .where(orm.SummaryCacheEntry.cache_key == key)
    ).scalar_one_or_none()

    def _persist(*, generated: bool, body: str, model: str | None, provider_type: str | None) -> None:
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
        existing.schema_version = PROMPT_VERSION
        existing.evidence_refs = {
            "refs": refs,
            "snapshot": evidence_text,
            "prompt_version": PROMPT_VERSION,
            "mode": mode,
        }
        existing.memory_fingerprint = fingerprint
        existing.created_at = dt.datetime.now(dt.UTC)
        existing.invalidated_at = None
        session.flush()

    cached_refs = (existing.evidence_refs or {}).get("refs", refs) if existing is not None else refs
    if existing is not None and not force and existing.invalidated_at is None:
        stale = existing.memory_fingerprint != fingerprint
        out = _result(
            generated=existing.generated,
            status="ok" if existing.generated else "llm_unavailable",
            briefing=existing.body if existing.generated else None,
            source_refs=cached_refs,
            model=existing.model,
            provider_type=existing.provider_type,
            cached=True,
            stale=stale,
            evidence_snapshot=(existing.evidence_refs or {}).get("snapshot"),
            note="Source records changed since this briefing was generated." if stale else None,
        )
        if not existing.generated:
            out.evidence = evidence  # deterministic fallback packet
        elif mode == "summary_with_evidence":
            out.evidence = evidence
        return out

    settings = session.get(orm.WorkspaceSettings, scope.workspace_id)
    resolved = model_config.resolve(session, scope.workspace_id, "summarization", secret_box=secret_box)
    provider = build_provider(resolved)

    def _fallback(status: str, note: str | None) -> BriefingResult:
        _persist(generated=False, body="", model=None, provider_type=None)
        return _result(
            generated=False,
            status=status,
            evidence=evidence,
            evidence_snapshot=evidence_text,
            note=note,
        )

    if provider is None:
        return _fallback(
            "llm_unavailable", "No summarization model is configured (Settings -> Model & provider)."
        )
    try:
        llm_usage.ensure_within_budget(session, scope.workspace_id, settings)  # type: ignore[arg-type]
    except LLMBudgetExceeded as exc:
        return _fallback("budget_exceeded", str(exc))

    out_cap = max_tokens or {"brief": 350, "standard": 700, "comprehensive": 1200}[detail_level]
    prompt = f"PURPOSE: {purpose}\nCOVERAGE: {detail_level}\n\nEVIDENCE for {domain.name}:\n{evidence_text}"
    started = dt.datetime.now(dt.UTC)
    try:
        result = await provider.generate(system=_SYSTEM, prompt=prompt, max_output_tokens=out_cap)
    except LLMNotConfigured as exc:
        return _fallback("llm_unavailable", str(exc))
    except LLMError as exc:
        llm_usage.record(session, scope.workspace_id, purpose="summarization", error=exc, run_ref=public_id)
        return _fallback("llm_error", f"The model call failed ({exc.category}).")
    latency_ms = int((dt.datetime.now(dt.UTC) - started).total_seconds() * 1000)

    body, used_refs = _split_source_refs(result.text, allowed=set(refs))
    usage_event = llm_usage.record(
        session,
        scope.workspace_id,
        purpose="summarization",
        result=result,
        price_override=resolved.price_override if resolved else None,
        run_ref=public_id,
    )
    _persist(generated=True, body=body, model=result.model, provider_type=result.provider_type)
    out = _result(
        generated=True,
        status="ok",
        briefing=body,
        source_refs=used_refs or refs,
        model=result.model,
        provider_type=result.provider_type,
        evidence_snapshot=evidence_text,
        system_prompt=_SYSTEM,
        user_prompt=prompt,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost_usd=(
            float(usage_event.estimated_cost_usd) if usage_event.estimated_cost_usd is not None else None
        ),
        reported_cost_usd=result.reported_cost_usd,
        latency_ms=latency_ms,
        stop_reason=result.stop_reason,
    )
    if mode == "summary_with_evidence":
        out.evidence = evidence
    return out


def _split_source_refs(text: str, *, allowed: set[str]) -> tuple[str, list[str]]:
    m = _SOURCE_LINE_RE.search(text)
    if not m:
        return text.strip(), []
    body = text[: m.start()].rstrip()
    claimed = _REF_RE.findall(m.group(1))
    used = [r for r in dict.fromkeys(claimed) if r in allowed]
    return body, used


# --- drill-down ---------------------------------------------------


def get_context_sources(scope: WorkspaceScope, *, public_id: str, source_refs: list[str]) -> dict[str, Any]:
    """Return the underlying records for `source_refs` (spec: the agent drills
    down by calling this with the refs from a briefing). Workspace- and
    person-scoped: refs are resolved only against this one person's record."""
    repo = PeopleRepository(scope)
    row = repo.require(public_id, include_archived=True)
    domain = mapping.person_to_domain(row)
    core = _core_fields(domain)
    facts = {f.id: f for f in domain.facts}
    interactions = {i.id: i for i in domain.interactions}

    resolved: list[dict[str, Any]] = []
    missing: list[str] = []
    for raw in source_refs:
        ref = raw.strip()
        if ref.startswith("field:"):
            ref = ref[len("field:") :]
        if ref in facts:
            f = facts[ref]
            resolved.append(
                {
                    "ref": f.id,
                    "type": "relationship" if f.related_person_id else "fact",
                    "category": f.category,
                    "text": f.text,
                    "date": f.date.to_string() if f.date else None,
                    "confidence": f.confidence.value,
                    "related_person_id": f.related_person_id,
                    "sources": [{"type": s.type, "id": s.id} for s in f.sources],
                }
            )
        elif ref in interactions:
            i = interactions[ref]
            resolved.append(
                {
                    "ref": i.id,
                    "type": "interaction",
                    "text": i.summary,
                    "date": i.date.to_string(),
                    "channel": i.channel,
                    "connection_level": i.connection_level,
                    "sources": [{"type": s.type, "id": s.id} for s in i.sources],
                }
            )
        elif ref in core:
            resolved.append({"ref": ref, "type": "field", "value": core[ref]})
        elif ref == "summary" and domain.summary:
            resolved.append({"ref": "summary", "type": "field", "value": domain.summary})
        else:
            missing.append(raw)

    return {
        "person_id": public_id,
        "person_name": domain.name,
        "sources": resolved,
        "missing_refs": missing,
    }


def invalidate_for_person(session: Session, workspace_id: uuid.UUID, person_id: uuid.UUID) -> int:
    from .summaries import invalidate_for_person as _inv

    return _inv(session, workspace_id, person_id)
