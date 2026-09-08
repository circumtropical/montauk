"""Anthropic API provider (spec 16.1 'low-cost hosted provider')."""

from __future__ import annotations

import anthropic

from ..base import LLMAuthError, LLMError, LLMRateLimited, LLMResult, LLMTimeout


class AnthropicAPIProvider:
    provider_type = "anthropic_api"

    def __init__(self, *, model: str, api_key: str, base_url: str | None = None) -> None:
        self.model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url or None, max_retries=2)

    async def generate(self, *, system: str, prompt: str, max_output_tokens: int = 1024) -> LLMResult:
        try:
            msg = await self._client.messages.create(
                model=self.model,
                max_tokens=max_output_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AuthenticationError as exc:
            raise LLMAuthError(str(exc)) from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMAuthError(str(exc)) from exc
        except anthropic.RateLimitError as exc:
            raise LLMRateLimited(str(exc)) from exc
        except anthropic.APITimeoutError as exc:
            raise LLMTimeout(str(exc)) from exc
        except anthropic.APIError as exc:
            raise LLMError(str(exc)) from exc

        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text")
        return LLMResult(
            text=text.strip(),
            input_tokens=msg.usage.input_tokens + (msg.usage.cache_read_input_tokens or 0),
            output_tokens=msg.usage.output_tokens,
            model=msg.model,
            provider_type=self.provider_type,
            stop_reason=msg.stop_reason,
        )

    async def healthcheck(self) -> LLMResult:
        return await self.generate(
            system="Reply with the single word: ok", prompt="ping", max_output_tokens=16
        )
