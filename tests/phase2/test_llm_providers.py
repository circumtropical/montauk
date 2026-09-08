"""LLM provider contract tests -- deterministic, no live calls (spec 32)."""

from __future__ import annotations

import json

import httpx
import pytest

from montauk.llm import pricing
from montauk.llm.base import (
    LLMAuthError,
    LLMError,
    LLMNotConfigured,
    LLMRateLimited,
)
from montauk.llm.factory import ResolvedModelConfig, build_provider
from montauk.llm.providers import cli as cli_mod
from montauk.llm.providers.anthropic_api import AnthropicAPIProvider
from montauk.llm.providers.cli import ClaudeCLIProvider
from montauk.llm.providers.fake import FakeProvider
from montauk.llm.providers.openai_compatible import OpenAICompatibleProvider


class TestFakeProvider:
    async def test_generates_and_records_calls(self):
        p = FakeProvider(responder=lambda s, u: f"S={s[:3]} U={u[:3]}")
        r = await p.generate(system="system text", prompt="prompt text")
        assert r.text == "S=sys U=pro"
        assert r.provider_type == "fake"
        assert r.input_tokens > 0 and r.output_tokens == 40
        assert p.calls == [("system text", "prompt text")]

    async def test_can_be_told_to_fail(self):
        p = FakeProvider(fail_with=LLMRateLimited("slow down"))
        with pytest.raises(LLMRateLimited):
            await p.generate(system="", prompt="x")


class TestOpenAICompatible:
    def _provider(self, handler):
        return OpenAICompatibleProvider(
            model="local-model",
            base_url="http://llm.local/v1",
            api_key="k",
            transport=httpx.MockTransport(handler),
        )

    async def test_parses_chat_completion(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "model": "local-model",
                    "choices": [{"message": {"content": " hi there "}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                },
            )

        r = await self._provider(handler).generate(system="be brief", prompt="hello")
        assert r.text == "hi there"
        assert r.input_tokens == 11 and r.output_tokens == 3
        assert seen["url"] == "http://llm.local/v1/chat/completions"
        assert seen["auth"] == "Bearer k"
        assert seen["body"]["messages"][0] == {"role": "system", "content": "be brief"}

    async def test_maps_auth_and_rate_limit(self):
        async def go(status):
            def handler(_req):
                return httpx.Response(status, text="nope")

            await self._provider(handler).generate(system="", prompt="x")

        with pytest.raises(LLMAuthError):
            await go(401)
        with pytest.raises(LLMRateLimited):
            await go(429)
        with pytest.raises(LLMError):
            await go(500)


class TestClaudeCLI:
    async def test_parses_json_output(self, monkeypatch):
        canned = {
            "is_error": False,
            "stop_reason": "end_turn",
            "result": "  A tidy summary.  ",
            "total_cost_usd": 0.0012,
            "usage": {"input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 20},
        }

        async def fake_run(argv, *, stdin, timeout, env=None):
            assert argv[0] == "claude" and "--system-prompt" in argv
            assert (env or {}).get("MAX_THINKING_TOKENS") == "0"  # extended thinking off
            return 0, json.dumps(canned), ""

        monkeypatch.setattr(cli_mod, "_run", fake_run)
        r = await ClaudeCLIProvider(model="claude-haiku-4-5").generate(system="s", prompt="p")
        assert r.text == "A tidy summary."
        assert r.input_tokens == 105 and r.output_tokens == 20
        assert r.reported_cost_usd == 0.0012

    async def test_error_output_raises(self, monkeypatch):
        async def fake_run(argv, *, stdin, timeout, env=None):
            return 1, json.dumps({"is_error": True, "result": "auth failed"}), ""

        monkeypatch.setattr(cli_mod, "_run", fake_run)
        with pytest.raises(LLMError):
            await ClaudeCLIProvider(model="x").generate(system="s", prompt="p")

    async def test_missing_binary_is_not_configured(self):
        with pytest.raises(LLMNotConfigured):
            await ClaudeCLIProvider(model="x", binary="definitely-not-a-real-binary-xyz").generate(
                system="s", prompt="p"
            )


class TestFactory:
    def test_none_config_yields_no_provider(self):
        assert build_provider(None) is None
        assert (
            build_provider(ResolvedModelConfig(purpose="summarization", provider_type="none", model=""))
            is None
        )

    def test_disabled_config_yields_no_provider(self):
        cfg = ResolvedModelConfig(
            purpose="summarization",
            provider_type="anthropic_api",
            model="claude-haiku-4-5",
            api_key="k",
            enabled=False,
        )
        assert build_provider(cfg) is None

    def test_builds_each_provider_type(self):
        anthropic_p = build_provider(
            ResolvedModelConfig(
                purpose="summarization",
                provider_type="anthropic_api",
                model="claude-haiku-4-5",
                api_key="sk-test",
            )
        )
        assert isinstance(anthropic_p, AnthropicAPIProvider)
        oai = build_provider(
            ResolvedModelConfig(
                purpose="extraction",
                provider_type="openai_compatible",
                model="m",
                base_url="http://x/v1",
            )
        )
        assert isinstance(oai, OpenAICompatibleProvider)
        assert isinstance(
            build_provider(
                ResolvedModelConfig(
                    purpose="summarization", provider_type="claude_cli", model="claude-opus-5"
                )
            ),
            ClaudeCLIProvider,
        )

    def test_missing_required_field_raises(self):
        with pytest.raises(LLMNotConfigured):
            build_provider(
                ResolvedModelConfig(purpose="summarization", provider_type="anthropic_api", model="m")
            )
        with pytest.raises(LLMNotConfigured):
            build_provider(
                ResolvedModelConfig(purpose="summarization", provider_type="openai_compatible", model="m")
            )


class TestPricing:
    def test_known_model_estimate(self):
        # haiku 4.5 = $1 / $5 per Mtok
        assert pricing.estimate_cost_usd(
            "claude-haiku-4-5", input_tokens=1_000_000, output_tokens=200_000
        ) == pytest.approx(1.0 + 1.0)

    def test_date_suffixed_model_still_matches(self):
        assert pricing.estimate_cost_usd(
            "claude-haiku-4-5-20251001", input_tokens=1_000_000, output_tokens=0
        ) == pytest.approx(1.0)

    def test_unknown_model_returns_none(self):
        assert pricing.estimate_cost_usd("some-local-llama", input_tokens=1000, output_tokens=1000) is None

    def test_override_wins(self):
        assert pricing.estimate_cost_usd(
            "some-local-llama", input_tokens=1_000_000, output_tokens=0, override=(2.0, 8.0)
        ) == pytest.approx(2.0)
