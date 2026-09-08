"""Subprocess providers that reuse a locally-installed, already-authenticated
coding-agent CLI: ``claude`` (Claude Code / Claude subscription) and
``codex`` (OpenAI Codex).

The CLI must be installed and signed in on the host -- that is the explicit
owner action spec 16.1 requires; no API key is read from the environment.
Because the dashboard runs sandboxed, the CLI's config dir may need to be
writable (see docs/deploy-phase2-shared-host.md / the service unit).
"""

from __future__ import annotations

import asyncio
import json
import shutil

from ..base import LLMError, LLMNotConfigured, LLMResult, LLMTimeout

_DEFAULT_TIMEOUT = 180.0


async def _run(argv: list[str], *, stdin: str, timeout: float) -> tuple[int, str, str]:
    if shutil.which(argv[0]) is None:
        raise LLMNotConfigured(f"{argv[0]!r} CLI is not installed on this host")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode("utf-8")), timeout)
    except TimeoutError as exc:
        raise LLMTimeout(f"{argv[0]} timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise LLMError(f"failed to run {argv[0]}: {exc}") from exc
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class ClaudeCLIProvider:
    provider_type = "claude_cli"

    def __init__(self, *, model: str, binary: str = "claude", timeout: float = _DEFAULT_TIMEOUT) -> None:
        self.model = model
        self._binary = binary
        self._timeout = timeout

    async def generate(self, *, system: str, prompt: str, max_output_tokens: int = 1024) -> LLMResult:
        argv = [
            self._binary,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--system-prompt",
            system,
            "--exclude-dynamic-system-prompt-sections",
            "--disallowedTools",
            "*",
        ]
        code, out, err = await _run(argv, stdin=prompt, timeout=self._timeout)
        try:
            data = json.loads(out)
        except ValueError as exc:
            raise LLMError(f"claude CLI returned non-JSON (exit {code}): {(err or out)[:300]}") from exc
        if data.get("is_error") or code != 0:
            raise LLMError(f"claude CLI error: {data.get('result') or err or 'unknown'}")
        usage = data.get("usage") or {}
        return LLMResult(
            text=str(data.get("result", "")).strip(),
            input_tokens=int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            model=self.model,
            provider_type=self.provider_type,
            stop_reason=data.get("stop_reason"),
            reported_cost_usd=data.get("total_cost_usd"),
        )

    async def healthcheck(self) -> LLMResult:
        return await self.generate(
            system="Reply with the single word: ok", prompt="ping", max_output_tokens=16
        )


class CodexCLIProvider:
    provider_type = "codex_cli"

    def __init__(self, *, model: str, binary: str = "codex", timeout: float = _DEFAULT_TIMEOUT) -> None:
        self.model = model
        self._binary = binary
        self._timeout = timeout

    async def generate(self, *, system: str, prompt: str, max_output_tokens: int = 1024) -> LLMResult:
        # `codex exec` runs a single non-interactive task and prints the final
        # message to stdout. It does not report token usage, so cost estimates
        # for this provider are reported as unavailable (spec 16.3).
        argv = [self._binary, "exec", "--model", self.model, "--skip-git-repo-check", "-"]
        combined = f"{system}\n\n---\n\n{prompt}"
        code, out, err = await _run(argv, stdin=combined, timeout=self._timeout)
        if code != 0:
            raise LLMError(f"codex CLI error (exit {code}): {(err or out)[:300]}")
        return LLMResult(
            text=out.strip(),
            input_tokens=0,
            output_tokens=0,
            model=self.model,
            provider_type=self.provider_type,
            stop_reason=None,
        )

    async def healthcheck(self) -> LLMResult:
        return await self.generate(
            system="Reply with the single word: ok", prompt="ping", max_output_tokens=16
        )
