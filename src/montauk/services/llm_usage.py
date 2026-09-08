"""LLM usage accounting and cost-control hard stop (spec 16.3, 18)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import models as orm
from ..llm.base import LLMBudgetExceeded, LLMError, LLMResult
from ..llm.pricing import estimate_cost_usd


@dataclass(frozen=True)
class UsageTotals:
    month: str
    calls: int
    ok_calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    reported_cost_usd: float

    @property
    def best_cost_usd(self) -> float:
        return self.reported_cost_usd or self.estimated_cost_usd


def _month_start(now: dt.datetime) -> dt.datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_to_date(
    session: Session, workspace_id: uuid.UUID, *, now: dt.datetime | None = None
) -> UsageTotals:
    now = now or dt.datetime.now(dt.UTC)
    start = _month_start(now)
    row = session.execute(
        select(
            func.count(orm.LLMUsageEvent.id),
            func.count(orm.LLMUsageEvent.id).filter(orm.LLMUsageEvent.ok.is_(True)),
            func.coalesce(func.sum(orm.LLMUsageEvent.input_tokens), 0),
            func.coalesce(func.sum(orm.LLMUsageEvent.output_tokens), 0),
            func.coalesce(func.sum(orm.LLMUsageEvent.estimated_cost_usd), 0),
            func.coalesce(func.sum(orm.LLMUsageEvent.reported_cost_usd), 0),
        )
        .where(orm.LLMUsageEvent.workspace_id == workspace_id)
        .where(orm.LLMUsageEvent.occurred_at >= start)
    ).one()
    return UsageTotals(
        month=start.strftime("%Y-%m"),
        calls=int(row[0]),
        ok_calls=int(row[1]),
        input_tokens=int(row[2]),
        output_tokens=int(row[3]),
        estimated_cost_usd=float(row[4]),
        reported_cost_usd=float(row[5]),
    )


@dataclass(frozen=True)
class BudgetState:
    paused: bool
    over_spend_limit: bool
    over_token_limit: bool
    spend_limit_usd: float | None
    token_limit: int | None
    usage: UsageTotals

    @property
    def blocked(self) -> bool:
        return self.paused or self.over_spend_limit or self.over_token_limit

    @property
    def reason(self) -> str | None:
        if self.paused:
            return "LLM processing is paused"
        if self.over_spend_limit:
            return f"monthly spend limit (${self.spend_limit_usd:.2f}) reached"
        if self.over_token_limit:
            return f"monthly token limit ({self.token_limit:,}) reached"
        return None


def budget_state(
    session: Session,
    workspace_id: uuid.UUID,
    settings: orm.WorkspaceSettings,
    *,
    now: dt.datetime | None = None,
) -> BudgetState:
    usage = month_to_date(session, workspace_id, now=now)
    spend_limit = float(settings.monthly_spend_limit_usd) if settings.monthly_spend_limit_usd else None
    token_limit = settings.monthly_token_limit
    return BudgetState(
        paused=bool(settings.llm_processing_paused),
        over_spend_limit=spend_limit is not None and usage.best_cost_usd >= spend_limit,
        over_token_limit=(
            token_limit is not None and (usage.input_tokens + usage.output_tokens) >= token_limit
        ),
        spend_limit_usd=spend_limit,
        token_limit=token_limit,
        usage=usage,
    )


def ensure_within_budget(session: Session, workspace_id: uuid.UUID, settings: orm.WorkspaceSettings) -> None:
    state = budget_state(session, workspace_id, settings)
    if state.blocked:
        raise LLMBudgetExceeded(state.reason or "over budget")


def record(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    purpose: str,
    result: LLMResult | None = None,
    error: LLMError | None = None,
    price_override: tuple[float, float] | None = None,
    run_ref: str | None = None,
) -> orm.LLMUsageEvent:
    if result is not None:
        est = estimate_cost_usd(
            result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            override=price_override,
        )
        event = orm.LLMUsageEvent(
            workspace_id=workspace_id,
            purpose=purpose,
            provider_type=result.provider_type,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            estimated_cost_usd=est,
            reported_cost_usd=result.reported_cost_usd,
            ok=True,
            run_ref=run_ref,
        )
    else:
        assert error is not None
        event = orm.LLMUsageEvent(
            workspace_id=workspace_id,
            purpose=purpose,
            provider_type="unknown",
            model="unknown",
            ok=False,
            error_category=error.category,
            run_ref=run_ref,
        )
    session.add(event)
    session.flush()
    return event
