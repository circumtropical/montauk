"""Deterministic in-process provider for tests (spec 32: 'Keep all provider
tests deterministic by default')."""

from __future__ import annotations

from collections.abc import Callable

from ..base import LLMError, LLMResult


class FakeProvider:
    provider_type = "fake"

    def __init__(
        self,
        *,
        model: str = "fake-1",
        responder: Callable[[str, str], str] | None = None,
        fail_with: LLMError | None = None,
        output_tokens: int = 40,
    ) -> None:
        self.model = model
        self._responder = responder or (lambda system, prompt: f"[fake summary] {prompt[:120]}")
        self._fail_with = fail_with
        self._output_tokens = output_tokens
        self.calls: list[tuple[str, str]] = []

    async def generate(self, *, system: str, prompt: str, max_output_tokens: int = 1024) -> LLMResult:
        self.calls.append((system, prompt))
        if self._fail_with is not None:
            raise self._fail_with
        text = self._responder(system, prompt)
        return LLMResult(
            text=text,
            input_tokens=max(1, len(system.split()) + len(prompt.split())),
            output_tokens=self._output_tokens,
            model=self.model,
            provider_type=self.provider_type,
            stop_reason="end_turn",
        )

    async def healthcheck(self) -> LLMResult:
        return await self.generate(system="", prompt="ping", max_output_tokens=8)
