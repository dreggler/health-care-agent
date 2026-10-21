from __future__ import annotations

import asyncio
import contextvars
import csv
import json
import logging
import random
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class LLMProvider(str, Enum):
    OPENAI         = "openai"
    AZURE_OPENAI   = "azure_openai"
    ANTHROPIC      = "anthropic"
    GEMINI         = "gemini"
    MISTRAL        = "mistral"
    GROQ           = "groq"
    DEEPSEEK       = "deepseek"
    XAI            = "xai"
    OPENROUTER     = "openrouter"
    PERPLEXITY     = "perplexity"
    GOOGLE_VERTEX  = "google_vertex"
    AMAZON_BEDROCK = "amazon_bedrock"
    MINIMAX        = "minimax"
    VLLM           = "vllm"
    ZHIPUAI        = "zhipuai"


def get_env_api_key(provider: str | LLMProvider) -> str | None:
    import os
    prov = provider.value if isinstance(provider, LLMProvider) else str(provider).lower()

    env_map = {
        "openai":            "OPENAI_API_KEY",
        "azure_openai":      "AZURE_OPENAI_API_KEY",
        "anthropic":         "ANTHROPIC_API_KEY",
        "gemini":            "GEMINI_API_KEY",
        "google":            "GEMINI_API_KEY",
        "groq":              "GROQ_API_KEY",
        "deepseek":          "DEEPSEEK_API_KEY",
        "xai":               "XAI_API_KEY",
        "openrouter":        "OPENROUTER_API_KEY",
        "perplexity":        "PERPLEXITY_API_KEY",
        "google_vertex":     "GOOGLE_CLOUD_API_KEY",
        "amazon_bedrock":    "AWS_SECRET_ACCESS_KEY",
        "mistral":           "MISTRAL_API_KEY",
        "cerebras":          "CEREBRAS_API_KEY",
        "vercel_ai_gateway": "AI_GATEWAY_API_KEY",
        "zai":               "ZAI_API_KEY",
        "minimax":           "MINIMAX_API_KEY",
        "minimax_cn":        "MINIMAX_CN_API_KEY",
        "huggingface":       "HF_TOKEN",
        "zhipuai":           "ZHIPUAI_API_KEY",
        "opencode":          "OPENCODE_API_KEY",
        "kimi_coding":       "KIMI_API_KEY",
    }

    if prov == "anthropic":
        return os.getenv("ANTHROPIC_OAUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")

    env_var = env_map.get(prov)
    return os.getenv(env_var) if env_var else None


_provider_registry: dict[str, type[BaseLLMProvider]] = {}


def register_provider(name: str | LLMProvider, cls: type[BaseLLMProvider]) -> None:
    key = name.value if isinstance(name, LLMProvider) else str(name).lower()
    _provider_registry[key] = cls


def get_provider_class(name: str | LLMProvider) -> type[BaseLLMProvider] | None:
    key = name.value if isinstance(name, LLMProvider) else str(name).lower()
    return _provider_registry.get(key)


_log_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "llm_log_context", default=None
)


def set_llm_log_context(
    user_id: str | None = None,
    loop: str | None = None,
    chat_id: str | None = None,
    tool_results: list[dict[str, Any]] | None = None,
) -> None:
    ctx: dict[str, Any] = {
        "user_id": user_id or "",
        "loop": loop or "",
        "chat_id": chat_id or "",
    }
    if tool_results is not None:
        ctx["tool_results"] = tool_results
    _log_context.set(ctx)


_MAX_FIELD_CHARS = 20_000
_MAX_LOG_BYTES = 64 * 1024 * 1024

_DATA_URI_RE = re.compile(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=\s]{64,}")


def _redact_blobs(text: str) -> str:
    return _DATA_URI_RE.sub(
        lambda m: f"<data-uri {len(m.group(0))} bytes redacted>", text,
    )


def _cap(text: str, limit: int = _MAX_FIELD_CHARS) -> str:
    if text and len(text) > limit:
        return f"{text[:limit]}… [truncated, {len(text)} chars total]"
    return text


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _MAX_LOG_BYTES:
            backup = path.with_name(path.name + ".1")
            backup.unlink(missing_ok=True)
            path.rename(backup)
    except OSError as exc:
        logger.warning("Could not rotate LLM usage log: %s", exc)


_CSV_COLUMNS = [
    "timestamp", "user_id", "loop", "chat_id",
    "provider", "model",
    "prompt_tokens", "completion_tokens", "thoughts_tokens", "response_tokens",
    "cache_read_tokens", "cache_creation_tokens", "total_tokens",
    "duration_ms",
    "tool_definitions", "input", "tool_calls", "tool_results", "response",
]
_csv_lock = threading.Lock()

_log_executor: ThreadPoolExecutor | None = None
_log_executor_lock = threading.Lock()


def _get_log_executor() -> ThreadPoolExecutor:
    global _log_executor
    if _log_executor is None:
        with _log_executor_lock:
            if _log_executor is None:
                _log_executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="llm-usage-log",
                )
    return _log_executor


def _get_log_path() -> Path:
    from ...config import settings
    if settings.LLM_USAGE_CSV_PATH is not None:
        path = settings.LLM_USAGE_CSV_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    settings.AGENT_LOGS_PATH.mkdir(parents=True, exist_ok=True)
    return settings.AGENT_LOGS_PATH / "llm_api.csv"


def write_usage_log(
    *,
    provider: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    thoughts_tokens: int | None = None,
    response_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    duration_ms: int | None = None,
    tool_definitions_str: str = "",
    input_summary: str,
    tool_calls_str: str,
    response_text: str = "",
) -> None:
    total = ""
    if prompt_tokens is not None or completion_tokens is not None:
        total = str(
            (prompt_tokens or 0) + (completion_tokens or 0) + (thoughts_tokens or 0)
        )
    ctx = _log_context.get() or {}
    tool_results = ctx.get("tool_results", [])
    tool_results_str = json.dumps(tool_results, ensure_ascii=False, indent=0) if tool_results else ""
    row = [
        datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),
        ctx.get("user_id", ""),
        ctx.get("loop", ""),
        ctx.get("chat_id", ""),
        provider,
        model,
        str(prompt_tokens) if prompt_tokens is not None else "",
        str(completion_tokens) if completion_tokens is not None else "",
        str(thoughts_tokens) if thoughts_tokens is not None else "",
        str(response_tokens) if response_tokens is not None else "",
        str(cache_read_tokens) if cache_read_tokens is not None else "",
        str(cache_creation_tokens) if cache_creation_tokens is not None else "",
        total,
        str(duration_ms) if duration_ms is not None else "",
        _cap(tool_definitions_str or ""),
        _cap(_redact_blobs(input_summary or "")),
        _cap(tool_calls_str or ""),
        _cap(_redact_blobs(tool_results_str)),
        _cap(response_text or ""),
    ]
    path = _get_log_path()
    with _csv_lock:
        _rotate_if_needed(path)
        file_exists = path.exists() and path.stat().st_size > 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_NONNUMERIC)
            if not file_exists:
                writer.writerow(_CSV_COLUMNS)
            writer.writerow(row)


def format_input_summary(messages: list[dict]) -> str:
    parts = []
    for i, m in enumerate(messages):
        content = m.get("content") or ""
        if isinstance(content, list):
            blocks: list[str] = []
            for b in content:
                if not isinstance(b, dict):
                    blocks.append(str(b))
                elif b.get("type") == "image_url":
                    url = (b.get("image_url") or {}).get("url", "")
                    blocks.append(f"<image {len(url)} bytes omitted>")
                elif b.get("type") == "image":
                    data = (b.get("source") or {}).get("data", "")
                    blocks.append(f"<image {len(data)} bytes omitted>")
                else:
                    blocks.append(str(b.get("text", b)))
            content = "\n".join(blocks)
        parts.append(f"[{i}] {m.get('role', '')}:\n{content}")
    return "\n---\n".join(parts)


def format_tool_definitions(tools: list[dict] | None) -> str:
    if not tools:
        return ""
    return json.dumps(tools, ensure_ascii=False, indent=0)


def format_tool_calls(tool_calls: list[dict]) -> str:
    if not tool_calls:
        return ""
    normalized = []
    for tc in tool_calls:
        args = tc.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                args = {"_raw": args}
        normalized.append({"name": tc.get("name", ""), "arguments": args})
    return json.dumps(normalized, ensure_ascii=False, indent=0)


_RETRY_MAX_ATTEMPTS = 5
_RETRY_BASE_DELAY   = 1.0
_RETRY_JITTER       = 0.25
_MAX_CAPACITY_RETRIES = 3


_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})
_CAPACITY_STATUS = frozenset({503, 529})
_RETRYABLE_KEYWORDS = (
    "unavailable", "rate limit", "rate_limit", "too many requests", "quota",
    "overloaded", "timeout", "timed out", "connection", "temporarily",
)
_CAPACITY_KEYWORDS = (
    "overloaded", "capacity", "unavailable", "high demand",
    "currently experiencing", "service_unavailable",
)
_OUT_OF_CREDIT_KEYWORDS = (
    "insufficient_quota", "credit balance is too low", "exceeded your current quota",
)


def _status_of(exc: Exception) -> int | None:
    from ..errors import _extract_status_code

    return _extract_status_code(exc)


def _is_out_of_credit(exc: Exception) -> bool:
    if _status_of(exc) == 402:
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in _OUT_OF_CREDIT_KEYWORDS)


def _is_retryable(exc: Exception) -> bool:
    from ..errors import is_context_overflow_message

    msg = str(exc).lower()
    status = _status_of(exc)
    if status in _RETRYABLE_STATUS:
        return True
    if is_context_overflow_message(msg):
        return False
    return any(kw in msg for kw in _RETRYABLE_KEYWORDS)


def _is_capacity_error(exc: Exception) -> bool:
    if _status_of(exc) in _CAPACITY_STATUS:
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in _CAPACITY_KEYWORDS)


def _is_rate_limit_error(exc: Exception) -> bool:
    if _status_of(exc) == 429:
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in ("rate limit", "rate_limit"))


async def retry_async(
    coro: Callable,
    max_retries: int = _RETRY_MAX_ATTEMPTS,
    on_retry: Callable | None = None,
) -> Any:
    from ..errors import FallbackTriggered, _extract_retry_after

    last_exc: Exception | None = None
    capacity_retries = 0

    for attempt in range(max_retries):
        try:
            return await coro()
        except Exception as exc:
            last_exc = exc
            if _is_out_of_credit(exc):
                logger.warning("LLM out of credit (%s) — switching to fallback provider", exc)
                raise FallbackTriggered("", "", reason=str(exc)) from exc
            if not _is_retryable(exc) or attempt == max_retries - 1:
                raise

            if _is_capacity_error(exc):
                capacity_retries += 1
                if capacity_retries >= _MAX_CAPACITY_RETRIES:
                    raise FallbackTriggered("", "", reason=str(exc)) from exc

            base = _RETRY_BASE_DELAY * (2 ** attempt)
            jitter = random.uniform(0, base * _RETRY_JITTER)
            delay = base + jitter

            retry_after = _extract_retry_after(exc)
            if retry_after and retry_after < 60:
                delay = max(delay, retry_after)

            error_type = "capacity" if _is_capacity_error(exc) else (
                "rate_limit" if _is_rate_limit_error(exc) else "transient"
            )

            logger.warning(
                "LLM %s error (attempt %d/%d): %s — retrying in %.1fs",
                error_type, attempt + 1, max_retries, exc, delay,
            )

            if on_retry:
                try:
                    await on_retry(attempt, delay, str(exc))
                except Exception:
                    pass

            await asyncio.sleep(delay)
    raise last_exc


def parse_image_data_uri(url: str) -> tuple[str, str]:
    try:
        if url.startswith("data:") and ";base64," in url:
            header, data = url.split(";base64,", 1)
            mime = header[len("data:"):].split(";")[0] or "image/jpeg"
            return mime, data
    except Exception:
        pass
    return "image/jpeg", ""


class BaseLLMProvider(ABC):

    def __init__(self, model: str, api_key: str | None = None) -> None:
        self.model   = model
        self.api_key = api_key

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = True,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        ...

    @abstractmethod
    def supports_tools(self) -> bool:
        ...

    def supports_vision(self) -> bool:
        return False


def provider_write_log(
    *,
    provider: str,
    model: str,
    prompt_tokens,
    completion_tokens,
    thoughts_tokens=None,
    response_tokens=None,
    cache_read_tokens=None,
    cache_creation_tokens=None,
    duration_ms=None,
    tools=None,
    messages: list[dict],
    tool_calls_list: list[dict],
    response_texts: list[str],
) -> None:
    resp_text = "\n".join(response_texts)
    if not resp_text and tool_calls_list:
        resp_text = "[Tool calls] " + ", ".join(tc.get("name", "?") for tc in tool_calls_list)

    def _do_write() -> None:
        try:
            write_usage_log(
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                thoughts_tokens=thoughts_tokens,
                response_tokens=response_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                duration_ms=duration_ms,
                tool_definitions_str=format_tool_definitions(tools) if tools else "",
                input_summary=format_input_summary(messages),
                tool_calls_str=format_tool_calls(tool_calls_list),
                response_text=resp_text,
            )
        except Exception as exc:
            logger.warning("Failed to write LLM usage log: %s", exc)

    try:
        _get_log_executor().submit(_do_write)
    except RuntimeError:
        _do_write()
