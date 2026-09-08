"""Build an :class:`LLMProvider` from a resolved model configuration."""

from __future__ import annotations

from dataclasses import dataclass

from .base import PROVIDER_TYPES, LLMNotConfigured, LLMProvider
from .providers.anthropic_api import AnthropicAPIProvider
from .providers.cli import ClaudeCLIProvider, CodexCLIProvider
from .providers.openai_compatible import OpenAICompatibleProvider


@dataclass(frozen=True)
class ResolvedModelConfig:
    """A model configuration with its secret already decrypted."""

    purpose: str
    provider_type: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    cli_binary: str | None = None
    price_input_per_mtok: float | None = None
    price_output_per_mtok: float | None = None
    enabled: bool = True

    @property
    def price_override(self) -> tuple[float, float] | None:
        if self.price_input_per_mtok is not None and self.price_output_per_mtok is not None:
            return (self.price_input_per_mtok, self.price_output_per_mtok)
        return None


def build_provider(config: ResolvedModelConfig | None) -> LLMProvider | None:
    if config is None or not config.enabled or config.provider_type in ("none", None):
        return None
    pt = config.provider_type
    if pt not in PROVIDER_TYPES:
        raise LLMNotConfigured(f"unknown provider type {pt!r}")

    if pt == "anthropic_api":
        if not config.api_key:
            raise LLMNotConfigured("anthropic_api needs an API key")
        return AnthropicAPIProvider(model=config.model, api_key=config.api_key, base_url=config.base_url)
    if pt == "openai_compatible":
        if not config.base_url:
            raise LLMNotConfigured("openai_compatible needs a base URL")
        return OpenAICompatibleProvider(model=config.model, base_url=config.base_url, api_key=config.api_key)
    if pt == "claude_cli":
        return ClaudeCLIProvider(model=config.model, binary=config.cli_binary or "claude")
    if pt == "codex_cli":
        return CodexCLIProvider(model=config.model, binary=config.cli_binary or "codex")
    raise LLMNotConfigured(f"provider type {pt!r} has no builder")
