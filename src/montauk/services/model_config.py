"""Per-purpose LLM model configuration (spec 16.1, 25.6).

A model is configured only by an explicit owner action here; there is no
ambient-env-key path. API keys are encrypted at rest via ``SecretBox``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import models as orm
from ..db.crypto import SecretBox
from ..llm.base import PROVIDER_TYPES, PURPOSES
from ..llm.factory import ResolvedModelConfig

# Shown as datalist suggestions in the Settings form; free text is allowed.
MODEL_SUGGESTIONS: dict[str, list[str]] = {
    "anthropic_api": ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
    "openai_compatible": ["gpt-4o-mini", "gpt-4o", "llama3.1", "qwen2.5", "mistral"],
    "claude_cli": ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
    "codex_cli": ["gpt-5-codex", "o4-mini"],
}
PROVIDER_LABELS = {
    "none": "Not configured",
    "anthropic_api": "Anthropic API (API key)",
    "openai_compatible": "OpenAI-compatible endpoint (base URL + key)",
    "claude_cli": "Local claude CLI (Claude Code / subscription auth)",
    "codex_cli": "Local codex CLI (OpenAI Codex / subscription auth)",
}


class ModelConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelConfigView:
    """Non-secret view for rendering the Settings form (the key is never sent
    back to the browser -- only whether one is stored)."""

    purpose: str
    provider_type: str
    model: str
    base_url: str
    cli_binary: str
    has_api_key: bool
    price_input_per_mtok: str
    price_output_per_mtok: str
    enabled: bool


def _f(value: Decimal | float | None) -> str:
    return "" if value is None else str(value)


def get_row(session: Session, workspace_id: uuid.UUID, purpose: str) -> orm.ModelConfiguration | None:
    return session.execute(
        select(orm.ModelConfiguration)
        .where(orm.ModelConfiguration.workspace_id == workspace_id)
        .where(orm.ModelConfiguration.purpose == purpose)
    ).scalar_one_or_none()


def view(session: Session, workspace_id: uuid.UUID, purpose: str) -> ModelConfigView:
    row = get_row(session, workspace_id, purpose)
    if row is None:
        return ModelConfigView(purpose, "none", "", "", "", False, "", "", True)
    return ModelConfigView(
        purpose=purpose,
        provider_type=row.provider_type,
        model=row.model or "",
        base_url=row.base_url or "",
        cli_binary=row.cli_binary or "",
        has_api_key=bool(row.api_key_ciphertext),
        price_input_per_mtok=_f(row.price_input_per_mtok),
        price_output_per_mtok=_f(row.price_output_per_mtok),
        enabled=row.enabled,
    )


def resolve(
    session: Session,
    workspace_id: uuid.UUID,
    purpose: str,
    *,
    secret_box: SecretBox | None,
) -> ResolvedModelConfig | None:
    row = get_row(session, workspace_id, purpose)
    if row is None or row.provider_type == "none" or not row.enabled:
        return None
    api_key: str | None = None
    if row.api_key_ciphertext:
        if secret_box is None:
            raise ModelConfigError("MONTAUK_MASTER_KEY is not set; cannot decrypt the stored API key")
        api_key = secret_box.decrypt(row.api_key_ciphertext)
    return ResolvedModelConfig(
        purpose=purpose,
        provider_type=row.provider_type,
        model=row.model,
        base_url=row.base_url,
        api_key=api_key,
        cli_binary=row.cli_binary,
        price_input_per_mtok=float(row.price_input_per_mtok) if row.price_input_per_mtok else None,
        price_output_per_mtok=float(row.price_output_per_mtok) if row.price_output_per_mtok else None,
        enabled=row.enabled,
    )


def _price(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        d = float(Decimal(raw))
    except (ArithmeticError, ValueError) as exc:
        raise ModelConfigError(f"price {raw!r} is not a number") from exc
    if d < 0:
        raise ModelConfigError("prices cannot be negative")
    return d


def save(
    session: Session,
    workspace_id: uuid.UUID,
    purpose: str,
    *,
    provider_type: str,
    model: str,
    base_url: str = "",
    cli_binary: str = "",
    api_key: str = "",
    clear_api_key: bool = False,
    price_input: str = "",
    price_output: str = "",
    enabled: bool = True,
    secret_box: SecretBox | None,
) -> None:
    if purpose not in PURPOSES:
        raise ModelConfigError(f"unknown purpose {purpose!r}")
    if provider_type not in PROVIDER_TYPES:
        raise ModelConfigError(f"unknown provider type {provider_type!r}")

    model = model.strip()
    base_url = base_url.strip()
    if provider_type != "none" and not model:
        raise ModelConfigError("a model name is required")
    if provider_type == "openai_compatible" and not base_url:
        raise ModelConfigError("an OpenAI-compatible endpoint needs a base URL")
    if provider_type == "anthropic_api" and not (api_key or _existing_key(session, workspace_id, purpose)):
        raise ModelConfigError("the Anthropic API provider needs an API key")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ModelConfigError("base URL must be an http(s) URL")

    row = get_row(session, workspace_id, purpose)
    if row is None:
        row = orm.ModelConfiguration(workspace_id=workspace_id, purpose=purpose)
        session.add(row)

    row.provider_type = provider_type
    row.model = model
    row.base_url = base_url or None
    row.cli_binary = cli_binary.strip() or None
    row.price_input_per_mtok = _price(price_input)
    row.price_output_per_mtok = _price(price_output)
    row.enabled = enabled

    if clear_api_key:
        row.api_key_ciphertext = None
    elif api_key.strip():
        if secret_box is None:
            raise ModelConfigError("MONTAUK_MASTER_KEY is not set; cannot store an API key")
        row.api_key_ciphertext = secret_box.encrypt(api_key.strip())
    session.flush()


def _existing_key(session: Session, workspace_id: uuid.UUID, purpose: str) -> bool:
    row = get_row(session, workspace_id, purpose)
    return bool(row and row.api_key_ciphertext)


def any_configured(session: Session, workspace_id: uuid.UUID) -> bool:
    return (
        session.execute(
            select(orm.ModelConfiguration.id)
            .where(orm.ModelConfiguration.workspace_id == workspace_id)
            .where(orm.ModelConfiguration.provider_type != "none")
            .where(orm.ModelConfiguration.enabled.is_(True))
        ).first()
        is not None
    )
