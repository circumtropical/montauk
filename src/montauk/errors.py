"""Typed, actionable tool errors (spec Appendix A).

Each subclass's `code` is a stable, machine-parseable prefix an agent can
switch on. Raising a MontaukError from a tool handler returns
`is_error=True` with the message in `content` (see mcp.server.mcpserver
.exceptions.ToolError, which this subclasses) -- the model sees the
message, and the server logs it at INFO without a traceback, since this
is an anticipated failure, not a crash.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError


class MontaukError(ToolError):
    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str):
        self.message = message
        super().__init__(f"{self.code}: {message}")


class NotFoundError(MontaukError):
    code = "NOT_FOUND"


class AmbiguousError(MontaukError):
    code = "AMBIGUOUS"


class MontaukValidationError(MontaukError):
    code = "VALIDATION_ERROR"


class PermissionDeniedError(MontaukError):
    code = "PERMISSION_DENIED"


class ArchivedError(MontaukError):
    code = "ARCHIVED"


class IndexDegradedError(MontaukError):
    code = "INDEX_DEGRADED"
