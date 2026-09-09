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
import contextlib
import json
import os
import shutil
import signal

from ..base import LLMError, LLMNotConfigured, LLMResult, LLMTimeout

_DEFAULT_TIMEOUT = 120.0
# `claude` sometimes leaves a short-lived helper process holding its stdout
# after the main process exits, so a read-to-EOF can hang indefinitely. Once
# the process itself has exited we wait at most this long for the pipes.
_PIPE_DRAIN_GRACE = 3.0


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGKILL)


async def _run(
    argv: list[str], *, stdin: str, timeout: float, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    if shutil.which(argv[0]) is None:
        raise LLMNotConfigured(f"{argv[0]!r} CLI is not installed on this host")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **env} if env else None,
            start_new_session=True,  # own process group, so we can kill any helpers it spawns
        )
    except OSError as exc:
        raise LLMError(f"failed to run {argv[0]}: {exc}") from exc
    pgid = proc.pid  # start_new_session -> the child leads a new group with this id

    assert proc.stdin and proc.stdout and proc.stderr
    p_stdin, p_stdout, p_stderr = proc.stdin, proc.stdout, proc.stderr

    async def _talk() -> tuple[bytes, bytes]:
        p_stdin.write(stdin.encode("utf-8"))
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await p_stdin.drain()
        p_stdin.close()
        out_task = asyncio.ensure_future(p_stdout.read())
        err_task = asyncio.ensure_future(p_stderr.read())
        await proc.wait()  # resolves on process exit even if a helper keeps the pipes open
        done, pending = await asyncio.wait({out_task, err_task}, timeout=_PIPE_DRAIN_GRACE)
        for t in pending:
            t.cancel()
        out = out_task.result() if out_task in done else b""
        err = err_task.result() if err_task in done else b""
        return out, err

    try:
        out, err = await asyncio.wait_for(_talk(), timeout)
    except TimeoutError as exc:
        raise LLMTimeout(f"{argv[0]} timed out after {timeout:.0f}s") from exc
    finally:
        _kill_group(pgid)  # nuke any lingering helper so no pipe/zombie is left behind
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class ClaudeCLIProvider:
    provider_type = "claude_cli"

    # Every Montauk LLM task is compression/extraction of supplied evidence, not
    # open-ended reasoning. Claude Code turns on extended thinking by default for
    # 4.5+ models, which spent ~8k thinking tokens (~90s) on a one-paragraph
    # briefing; MAX_THINKING_TOKENS=0 turns it off and the same call runs in ~6s.
    _ENV = {"MAX_THINKING_TOKENS": "0"}

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
        code, out, err = await _run(argv, stdin=prompt, timeout=self._timeout, env=self._ENV)
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
