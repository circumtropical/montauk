"""OpenAI-compatible chat-completions provider (spec 16.1): OpenAI,
OpenRouter, Ollama, llama.cpp, vLLM, Anthropic's OpenAI-compat endpoint, ...
Configured with a base URL + model name + (optional) API key.
"""

from __future__ import annotations

import httpx

from ..base import LLMAuthError, LLMError, LLMRateLimited, LLMResult, LLMTimeout


class OpenAICompatibleProvider:
    provider_type = "openai_compatible"

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or None
        self._transport = transport

    async def generate(self, *, system: str, prompt: str, max_output_tokens: int = 1024) -> LLMResult:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self.model,
            "max_tokens": max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=120.0, transport=self._transport) as client:
                resp = await client.post(f"{self._base_url}/chat/completions", json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMTimeout(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise LLMError(str(exc)) from exc

        if resp.status_code in (401, 403):
            raise LLMAuthError(f"{resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 429:
            raise LLMRateLimited(resp.text[:200])
        if resp.status_code >= 400:
            raise LLMError(f"{resp.status_code}: {resp.text[:300]}")

        try:
            data = resp.json()
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
            usage = data.get("usage") or {}
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"unparseable response: {resp.text[:300]}") from exc

        return LLMResult(
            text=text.strip(),
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            model=str(data.get("model", self.model)),
            provider_type=self.provider_type,
            stop_reason=choice.get("finish_reason"),
        )

    async def healthcheck(self) -> LLMResult:
        return await self.generate(
            system="Reply with the single word: ok", prompt="ping", max_output_tokens=16
        )
