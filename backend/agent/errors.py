from __future__ import annotations

import asyncio
import re
from enum import Enum

_STATUS_CODE_RES = {
    code: re.compile(rf"\b{code}\b")
    for code in (429, 529, 503, 500, 502, 504, 400, 401, 403, 404)
}

_MAX_TOKEN_RE = re.compile(r"max\w*[\s_-]*(?:\w+[\s_-]+)?tokens?")

_CONTEXT_OVERFLOW_PATTERNS = (
    "prompt is too long",
    "prompt too long",
    "input token count",
    "exceeds the maximum number of tokens",
    "maximum context length",
    "context_length_exceeded",
    "context length exceeded",
    "context window",
    "reduce the length of the messages",
    "input is too long",
)


def is_context_overflow_message(msg: str) -> bool:
    low = (msg or "").lower()
    if any(p in low for p in _CONTEXT_OVERFLOW_PATTERNS):
        return True
    return (
        any(kw in low for kw in ("context", "prompt too long", "token limit"))
        or bool(_MAX_TOKEN_RE.search(low))
    ) and any(kw in low for kw in ("long", "length", "exceed", "limit", "overflow"))


class ErrorCategory(Enum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    CAPACITY = "capacity"
    VALIDATION = "validation"
    PERMISSION = "permission"
    CONTEXT_OVERFLOW = "context_overflow"
    TOOL_BUG = "tool_bug"
    LLM_ERROR = "llm_error"


class AgentError:

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        recoverable: bool = True,
        original: Exception | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.category = category
        self.message = message
        self.recoverable = recoverable
        self.original = original
        self.status_code = status_code
        self.retry_after = retry_after

    def to_tool_result(self) -> dict:
        return {
            "success": False,
            "error": self.message,
            "error_type": self.category.value,
            "recoverable": self.recoverable,
        }

    def __repr__(self) -> str:
        return f"AgentError({self.category.value}: {self.message})"


class FallbackTriggered(Exception):

    def __init__(self, provider: str, model: str = "", reason: str = "") -> None:
        self.provider = provider
        self.model = model
        self.reason = reason
        text = f"Fallback triggered: {provider}/{model}"
        if reason:
            text += f" ({reason})"
        super().__init__(text)


def _extract_status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val

    resp = getattr(exc, "response", None)
    if resp is not None:
        for attr in ("status_code", "status"):
            val = getattr(resp, attr, None)
            if isinstance(val, int):
                return val

    msg = str(exc)
    for code, pattern in _STATUS_CODE_RES.items():
        if pattern.search(msg):
            return code

    return None


def _extract_retry_after(exc: Exception) -> float | None:
    resp = getattr(exc, "response", None)
    if resp is None:
        return None

    headers = getattr(resp, "headers", None)
    if headers is None:
        return None

    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            return float(retry_after)
        except (ValueError, TypeError):
            pass

    reset = headers.get("Anthropic-Ratelimit-Unified-Reset")
    if reset:
        try:
            from datetime import datetime, timezone
            reset_time = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            delta = (reset_time - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, delta)
        except (ValueError, TypeError):
            pass

    return None


def classify_error(error: Exception, context: str = "") -> AgentError:
    msg = str(error).lower()
    status = _extract_status_code(error)
    retry_after = _extract_retry_after(error)

    if isinstance(error, (asyncio.TimeoutError,)):
        return AgentError(
            ErrorCategory.TRANSIENT,
            f"Operation timed out: {context}",
            retry_after=retry_after,
            original=error,
        )

    if status not in (429, 503, 529) and is_context_overflow_message(msg):
        return AgentError(
            ErrorCategory.CONTEXT_OVERFLOW,
            "Context window exceeded",
            original=error,
        )

    if status == 429 or any(kw in msg for kw in ("rate limit", "rate_limit", "quota exceeded")):
        return AgentError(
            ErrorCategory.RATE_LIMITED,
            "Rate limited — will retry after backoff",
            status_code=status,
            retry_after=retry_after,
            original=error,
        )

    if status in (529, 503) or any(kw in msg for kw in ("overloaded", "capacity")):
        return AgentError(
            ErrorCategory.CAPACITY,
            "Service overloaded",
            status_code=status,
            retry_after=retry_after,
            original=error,
        )

    if is_context_overflow_message(msg):
        return AgentError(
            ErrorCategory.CONTEXT_OVERFLOW,
            "Context window exceeded",
            original=error,
        )

    if status in (500, 502, 504) or any(kw in msg for kw in (
        "unavailable", "timeout", "connection", "temporarily", "internal server",
    )):
        return AgentError(
            ErrorCategory.TRANSIENT,
            f"Transient error: {error}",
            status_code=status,
            retry_after=retry_after,
            original=error,
        )

    if status in (401, 403) or any(kw in msg for kw in (
        "permission", "not authorized", "unauthorized", "forbidden", "authentication",
    )):
        return AgentError(
            ErrorCategory.PERMISSION,
            str(error),
            recoverable=False,
            status_code=status,
            original=error,
        )

    if isinstance(error, (ValueError, KeyError, TypeError)):
        return AgentError(
            ErrorCategory.VALIDATION,
            str(error),
            original=error,
        )

    return AgentError(
        ErrorCategory.TOOL_BUG,
        str(error),
        original=error,
    )
