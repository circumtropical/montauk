"""Turn newly-archived transcript messages into curated memory (spec 17).

Runs per thread, in local-calendar-day batches, against the configured
*extraction* model. Facts and one summarized interaction per person per
active day are written at ``automatically_extracted`` authority -- the
lowest, so a later owner or agent edit always wins. Conservative: only
direct statements and straightforward implications, uncertainty
preserved, no tasks / predictions / diagnoses. Nothing runs without a
configured model; nothing is written from a heuristic.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import mapping
from ..db import models as orm
from ..db.crypto import SecretBox
from ..db.repositories import PeopleRepository, WorkspaceScope
from ..llm.base import LLMBudgetExceeded, LLMError, LLMNotConfigured
from ..llm.factory import build_provider
from ..schema import CATEGORIES
from ..tokens import estimate_tokens
from . import llm_usage, model_config

PROMPT_VERSION = "extract.v1"
_CONF = {"high", "medium", "low"}

_SYSTEM = """\
You extract durable relationship facts from a batch of chat messages from ONE
conversation on ONE day, for the owner of a private relationship-memory system.

Output STRICT JSON only, no prose, no code fence:
{"facts": [{"person_id": "P0001", "category": "<one of the fixed categories>",
            "text": "<a single concise factual sentence>", "confidence": "high|medium|low",
            "date": "YYYY-MM-DD" | null}],
 "interactions": {"P0001": "<one or two sentences summarizing the day's exchange with this person>"}}

Fixed categories: Family, Work & Education, Interests, Relationship with User,
Life Events, General Notes.

Rules:
- Only facts about a person in the roster, stated by them or the owner about them.
  Use their person_id. Never invent a person or a person_id.
- Accept direct statements and straightforward implications. Do NOT extract from
  jokes, sarcasm, quoted or forwarded text, speculation, or third-party claims.
- Preserve uncertainty in wording and in `confidence`. Vague stays vague.
- No tasks, reminders, commitments, to-dos, plans, predictions, diagnoses, or
  psychological judgements. No unsupported relationship claims.
- `date` is the date the fact refers to if clearly stated, else null.
- An interaction summary only for a person with a meaningful exchange that day.
  Omit trivial reactions, automation, or one-word replies. No summary => omit the key.
- If nothing qualifies: {"facts": [], "interactions": {}}.
"""


@dataclass
class PersonOutcome:
    person_id: str
    name: str
    facts_added: int = 0
    interaction_days: int = 0


@dataclass
class ExtractionResult:
    status: str  # ok | partial | model_unavailable | budget_exceeded | no_mapping | nothing_pending | error
    days_processed: int = 0
    messages_processed: int = 0
    facts_added: int = 0
    interactions_touched: int = 0
    awaiting_remaining: int = 0
    per_person: list[PersonOutcome] = field(default_factory=list)
    note: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_model_json(text: str) -> dict[str, Any]:
    m = _JSON_RE.search(text)
    if not m:
        raise ValueError("model returned no JSON object")
    return json.loads(m.group(0))


def _format_messages(msgs: list[orm.SourceMessage], name_by_norm: dict[str, str]) -> str:
    lines = []
    for msg in msgs:
        who = name_by_norm.get(msg.sender_normalized or "", msg.sender_name or "?")
        body = "[media omitted]" if msg.media_omitted else msg.text.replace("\n", " ").strip()
        lines.append(f"[{msg.sent_at:%H:%M}] {who}: {body}")
    return "\n".join(lines)


def _existing_fact_keys(domain: Any) -> set[str]:
    return {" ".join(f.text.split()).casefold() for f in domain.facts}


async def run_extraction(
    session: Session,
    scope: WorkspaceScope,
    *,
    thread_id: uuid.UUID,
    secret_box: SecretBox | None,
    max_days: int = 4,
) -> ExtractionResult:
    ws = scope.workspace_id
    parts = (
        session.execute(select(orm.SourceParticipant).where(orm.SourceParticipant.thread_id == thread_id))
        .scalars()
        .all()
    )
    owner_norms = {p.display_name_normalized for p in parts if p.role == "owner"}
    person_by_norm: dict[str, orm.Person] = {}
    repo = PeopleRepository(scope)
    for p in parts:
        if p.role == "person" and p.person_id is not None:
            row = repo.get_by_uuid(p.person_id)
            if row is not None:
                person_by_norm[p.display_name_normalized] = row
    if not person_by_norm:
        return ExtractionResult(status="no_mapping", note="Map at least one participant to a person first.")

    name_by_norm = {
        **{n: "the owner" for n in owner_norms},
        **{n: f"{row.name} ({row.public_id})" for n, row in person_by_norm.items()},
    }
    people_by_pid = {row.public_id: row for row in person_by_norm.values()}

    settings = session.get(orm.WorkspaceSettings, ws)
    if not model_config.is_configured(session, ws, "extraction"):
        return ExtractionResult(
            status="model_unavailable",
            note="Configure an extraction model in Settings -> Model & provider.",
        )
    resolved = model_config.resolve(session, ws, "extraction", secret_box=secret_box)
    provider = build_provider(resolved)
    if provider is None:
        return ExtractionResult(status="model_unavailable", note="No extraction model is configured.")
    try:
        llm_usage.ensure_within_budget(session, ws, settings)  # type: ignore[arg-type]
    except LLMBudgetExceeded as exc:
        return ExtractionResult(status="budget_exceeded", note=str(exc))

    pending = list(
        session.execute(
            select(orm.SourceMessage)
            .where(orm.SourceMessage.thread_id == thread_id)
            .where(orm.SourceMessage.processing_status == "awaiting_processing")
            .order_by(orm.SourceMessage.sent_at)
        ).scalars()
    )
    if not pending:
        return ExtractionResult(status="nothing_pending", note="No messages are awaiting extraction.")

    by_day: dict[dt.date, list[orm.SourceMessage]] = defaultdict(list)
    for msg in pending:
        by_day[msg.sent_at.date()].append(msg)

    max_input = (settings.max_job_input_tokens if settings else 60_000) or 60_000
    outcomes: dict[str, PersonOutcome] = {
        row.public_id: PersonOutcome(row.public_id, row.name) for row in people_by_pid.values()
    }
    result = ExtractionResult(status="ok")
    days = sorted(by_day)

    for day in days[:max_days]:
        msgs = by_day[day]
        # Every mapped person gets their current facts in context (small n).
        context_blocks = []
        for row in people_by_pid.values():
            d = mapping.person_to_domain(row)
            facts = "; ".join(f.text for f in d.facts[:8]) or "(no facts recorded yet)"
            context_blocks.append(f"{row.name} ({row.public_id}) -- known: {facts}")

        convo = _format_messages(msgs, name_by_norm)
        if estimate_tokens(convo) > max_input:
            keep: list[orm.SourceMessage] = []
            used = 0
            for m in msgs:
                used += estimate_tokens(m.text) + 12
                if used > max_input:
                    break
                keep.append(m)
            convo = _format_messages(keep, name_by_norm)
            msgs = keep
            result.note = "Some day-batches were truncated to the max job input size."

        roster = "\n".join(context_blocks) or "(no facts on file for the mapped people yet)"
        prompt = (
            f"ROSTER (owner = 'the owner'; extract only about these people):\n{roster}\n\n"
            f"CONVERSATION on {day.isoformat()}:\n{convo}"
        )
        try:
            r = await provider.generate(system=_SYSTEM, prompt=prompt, max_output_tokens=1500)
        except (LLMNotConfigured, LLMError) as exc:
            llm_usage.record(session, ws, purpose="extraction", error=exc, run_ref=f"extract:{thread_id}")
            result.status = "partial" if result.days_processed else "error"
            result.note = f"Extraction stopped after a model error ({getattr(exc, 'category', 'error')})."
            break

        usage = llm_usage.record(
            session,
            ws,
            purpose="extraction",
            result=r,
            price_override=resolved.price_override if resolved else None,
            run_ref=f"extract:{thread_id}",
        )
        result.input_tokens += r.input_tokens
        result.output_tokens += r.output_tokens
        if usage.estimated_cost_usd is not None:
            result.estimated_cost_usd = (result.estimated_cost_usd or 0.0) + float(usage.estimated_cost_usd)

        try:
            data = _parse_model_json(r.text)
        except ValueError:
            result.note = "The model returned unparseable output for at least one day; skipped it."
            continue

        for raw in data.get("facts", []) or []:
            pid = str(raw.get("person_id", "")).strip()
            row = people_by_pid.get(pid)
            category = str(raw.get("category", "")).strip()
            text = " ".join(str(raw.get("text", "")).split())
            conf = str(raw.get("confidence", "medium")).strip().lower()
            if row is None or category not in CATEGORIES or len(text) < 3:
                continue
            domain = mapping.person_to_domain(row)
            if " ".join(text.split()).casefold() in _existing_fact_keys(domain):
                continue
            repo.add_fact(
                row,
                category=category,
                text=text,
                date=str(raw["date"]) if raw.get("date") else None,
                confidence=conf if conf in _CONF else "medium",
                reason=f"extracted from WhatsApp transcript ({day.isoformat()})",
                authority="automatically_extracted",
            )
            result.facts_added += 1
            outcomes[pid].facts_added += 1

        for pid, summary in (data.get("interactions", {}) or {}).items():
            row = people_by_pid.get(str(pid).strip())
            summary = " ".join(str(summary).split())
            if row is None or len(summary) < 3:
                continue
            existing = next(
                (
                    i
                    for i in row.interactions
                    if i.channel == "whatsapp" and (i.date_text or "").startswith(day.isoformat())
                ),
                None,
            )
            if existing is not None:
                repo.update_interaction(
                    row,
                    existing.local_id,
                    summary=summary,
                    reason=f"re-extracted from WhatsApp ({day.isoformat()})",
                )
            else:
                repo.add_interaction(
                    row,
                    date=day.isoformat(),
                    channel="whatsapp",
                    summary=summary,
                    reason=f"extracted from WhatsApp transcript ({day.isoformat()})",
                    authority="automatically_extracted",
                )
            result.interactions_touched += 1
            outcomes[pid].interaction_days += 1

        for m in msgs:
            m.processing_status = "processed"
        result.days_processed += 1
        result.messages_processed += len(msgs)
        # Commit each day as it finishes: a wedged model call on a later batch
        # then can't lose this day's facts or hold its row locks open.
        session.commit()

    session.flush()
    result.awaiting_remaining = (
        session.execute(
            select(orm.SourceMessage.id)
            .where(orm.SourceMessage.thread_id == thread_id)
            .where(orm.SourceMessage.processing_status == "awaiting_processing")
        )
        .scalars()
        .all()
        .__len__()
    )
    if result.status == "ok" and result.awaiting_remaining:
        result.status = "partial"
        result.note = result.note or f"{result.awaiting_remaining} messages still pending -- run again."
    result.per_person = [o for o in outcomes.values() if o.facts_added or o.interaction_days]
    return result
