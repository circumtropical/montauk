"""Provider-neutral LLM interface for summarization and (later) extraction
(spec 16.1).

Nothing here bundles model weights or requires local inference hardware.
A provider is only ever constructed from an explicit owner configuration
(``model_configurations``), never from an ambient API key (spec 16.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Purposes that can each point at their own model (spec 16.1: "separate
# extraction and summarization model settings").
PURPOSES = ("summarization", "extraction")

PROVIDER_TYPES = (
    "none",
    "anthropic_api",
    "openai_compatible",
    "claude_cli",
    "codex_cli",
)


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    provider_type: str
    stop_reason: str | None = None
    # Some providers (the Claude CLI) report their own cost; when present it
    # is authoritative over the pricing-table estimate.
    reported_cost_usd: float | None = None


class LLMError(RuntimeError):
    """Base for every provider failure. ``category`` is a stable slug an
    operator/agent can branch on (spec 18: 'error category')."""

    category = "provider_error"


class LLMNotConfigured(LLMError):
    category = "not_configured"


class LLMAuthError(LLMError):
    category = "auth"


class LLMRateLimited(LLMError):
    category = "rate_limit"


class LLMTimeout(LLMError):
    category = "timeout"


class LLMBudgetExceeded(LLMError):
    category = "budget_exceeded"


@runtime_checkable
class LLMProvider(Protocol):
    provider_type: str
    model: str

    async def generate(self, *, system: str, prompt: str, max_output_tokens: int = 1024) -> LLMResult:
        """One completion. Raises an :class:`LLMError` subclass on any failure;
        never returns partial or fabricated text."""
        ...

    async def healthcheck(self) -> LLMResult:
        """A tiny live call used by the Settings 'test connection' button.
        Raises on failure."""
        ...
