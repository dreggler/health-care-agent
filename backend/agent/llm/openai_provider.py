from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from . import (
    BaseLLMProvider,
    provider_write_log,
    retry_async,
)

logger = logging.getLogger(__name__)


_FIRST_CHUNK_MIN_TIMEOUT = 300.0


async def _stall_guarded(stream, timeout: float, model: str):
    from ..errors import FallbackTriggered

    it = stream.__aiter__()
    first = True
    while True:
        budget = max(timeout, _FIRST_CHUNK_MIN_TIMEOUT) if first else timeout
        try:
            if timeout > 0:
                chunk = await asyncio.wait_for(it.__anext__(), budget)
            else:
                chunk = await it.__anext__()
            first = False
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            try:
                await stream.close()
            except Exception:
                pass
            logger.warning(
                "LLM stream stalled: %s sent no chunk for %.0fs — aborting",
                model, budget,
            )
            raise FallbackTriggered("", model, reason=f"no chunk for {budget:.0f}s")
        yield chunk


_INTERNAL_MESSAGE_KEYS = ("signature", "_tool_name")


def _wire_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if any(k in m for k in _INTERNAL_MESSAGE_KEYS):
            m = {k: v for k, v in m.items() if k not in _INTERNAL_MESSAGE_KEYS}
        out.append(m)
    return out


class OpenAIProvider(BaseLLMProvider):

    _SUPPORTED_PREFIXES = ("gpt-4.5", "gpt-5", "o3", "o4", "chatgpt-4o")

    _is_vllm: bool = False

    _is_deepseek: bool = False

    def __init__(
        self,
        model: str = "gpt-5.4-mini",
        api_key: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(model, api_key)
        self._extra = kwargs
        try:
            import httpx
            from openai import AsyncOpenAI
            kwargs.setdefault(
                "timeout",
                httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
            )
            self._client = AsyncOpenAI(api_key=api_key, **kwargs)
        except ImportError as exc:
            raise ImportError(
                "openai package required. Install: pip install openai>=1.50"
            ) from exc

    def supports_tools(self) -> bool:
        return True

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = True,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        _t0 = time.perf_counter()
        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": _wire_messages(messages),
                "temperature": temperature,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if max_tokens is not None:
                kwargs["max_completion_tokens"] = max_tokens
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            if self._is_vllm:
                model_lower = self.model.lower()
                if "glm" in model_lower or "qwen" in model_lower or "gemma" in model_lower:
                    kwargs.setdefault("extra_body", {})
                    kwargs["extra_body"]["chat_template_kwargs"] = {
                        "enable_thinking": False,
                    }
            elif self._is_deepseek:
                from backend.config import settings
                thinking = (settings.DEEPSEEK_THINKING or "").strip().lower()
                if thinking in ("enabled", "disabled"):
                    kwargs.setdefault("extra_body", {})
                    kwargs["extra_body"]["thinking"] = {"type": thinking}
                kwargs["max_tokens"] = (
                    kwargs.pop("max_completion_tokens", None)
                    or settings.DEEPSEEK_MAX_TOKENS
                )
                if max_tokens is None:
                    max_tokens = kwargs["max_tokens"]
                ds_effort = (settings.DEEPSEEK_REASONING_EFFORT or "").strip()
                if ds_effort:
                    kwargs["reasoning_effort"] = ds_effort
            else:
                from backend.config import settings
                effort = (settings.OPENAI_REASONING_EFFORT or "").strip()
                if effort:
                    kwargs["reasoning_effort"] = effort
                    kwargs.pop("temperature", None)
                    if effort.lower() == "minimal" and tools:
                        kwargs["parallel_tool_calls"] = False

            async def _call():
                return await self._client.chat.completions.create(**kwargs)

            try:
                stream_ctx = await retry_async(_call)
            except Exception as ctx_err:
                err_str = str(ctx_err)
                if "too large" in err_str and "input tokens" in err_str:
                    import re
                    m = re.search(r"context length is (\d+).*?(\d+) input tokens", err_str)
                    if m:
                        ctx_limit = int(m.group(1))
                        input_tokens = int(m.group(2))
                        safe_max = max(ctx_limit - input_tokens - 64, 256)
                        logger.warning(
                            "Context window exceeded: %d input + %d max_tokens > %d limit. "
                            "Retrying with max_tokens=%d",
                            input_tokens, max_tokens, ctx_limit, safe_max,
                        )
                        cap_key = "max_tokens" if self._is_deepseek else "max_completion_tokens"
                        kwargs[cap_key] = safe_max
                        max_tokens = safe_max
                        stream_ctx = await retry_async(_call)
                    else:
                        raise
                else:
                    raise

            tool_accum: dict[int, dict[str, str]] = {}
            content_parts: list[str] = []
            thought_parts: list[str] = []
            prompt_tokens = completion_tokens = thoughts_tokens = None
            finish_reason: str | None = None
            cache_read_tokens: int | None = None

            from backend.config import settings as _settings
            stall_timeout = float(_settings.LLM_STREAM_STALL_TIMEOUT or 0)
            async for chunk in _stall_guarded(stream_ctx, stall_timeout, self.model):
                if chunk.usage:
                    prompt_tokens     = chunk.usage.prompt_tokens
                    completion_tokens = chunk.usage.completion_tokens
                    details = getattr(chunk.usage, "completion_tokens_details", None)
                    if details:
                        thoughts_tokens = getattr(details, "reasoning_tokens", None)
                        if thoughts_tokens is None and isinstance(details, dict):
                            thoughts_tokens = details.get("reasoning_tokens")
                    prompt_details = getattr(chunk.usage, "prompt_tokens_details", None)
                    if prompt_details is not None:
                        cached = getattr(prompt_details, "cached_tokens", None)
                        if cached is None and isinstance(prompt_details, dict):
                            cached = prompt_details.get("cached_tokens")
                        if cached is not None:
                            cache_read_tokens = cached
                    if cache_read_tokens is None:
                        ds_hit = getattr(chunk.usage, "prompt_cache_hit_tokens", None)
                        if ds_hit is None:
                            extra_usage = getattr(chunk.usage, "model_extra", None) or {}
                            ds_hit = extra_usage.get("prompt_cache_hit_tokens")
                        if ds_hit is not None:
                            cache_read_tokens = ds_hit

                choice = chunk.choices[0] if chunk.choices else None
                if choice is None:
                    continue

                delta = choice.delta

                if delta.content:
                    content_parts.append(delta.content)
                    yield {"type": "content", "content": delta.content}

                reasoning = (
                    getattr(delta, "reasoning_content", None)
                    or getattr(delta, "reasoning", None)
                )
                if not reasoning and getattr(delta, "model_extra", None):
                    reasoning = delta.model_extra.get("reasoning_content") or delta.model_extra.get("reasoning")
                if reasoning:
                    thought_parts.append(reasoning)
                    yield {"type": "agent_thinking", "content": reasoning}

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_accum:
                            tool_accum[idx] = {
                                "id":        tc_delta.id or "",
                                "name":      "",
                                "arguments": "",
                            }
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tool_accum[idx]["name"] += tc_delta.function.name
                            if tc_delta.function.arguments:
                                tool_accum[idx]["arguments"] += tc_delta.function.arguments
                                yield {
                                    "type": "tool_call_delta",
                                    "index": idx,
                                    "name": tool_accum[idx]["name"],
                                    "arguments_delta": tc_delta.function.arguments,
                                }

                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                if choice.finish_reason in ("tool_calls", "stop", "length"):
                    for tc_info in tool_accum.values():
                        try:
                            args = json.loads(tc_info["arguments"]) if tc_info["arguments"] else {}
                        except json.JSONDecodeError:
                            logger.warning(
                                "Could not parse tool arguments for %s: %s",
                                tc_info["name"], tc_info["arguments"][:200],
                            )
                            args = {"_raw": tc_info["arguments"]}
                        tc = {"id": tc_info["id"], "name": tc_info["name"], "arguments": args}
                        yield {"type": "tool_call", **tc}

                if hasattr(chunk, 'choices') and choice and choice.delta:
                    extra = getattr(choice.delta, "model_extra", {}) or {}
                    _tt = extra.get("thoughts_tokens") or extra.get("reasoning_tokens")
                    if _tt:
                        thoughts_tokens = _tt

            if thoughts_tokens is None and thought_parts:
                reasoning_text = "".join(thought_parts)
                thoughts_tokens = max(1, len(reasoning_text) // 3)

            if prompt_tokens is not None or completion_tokens is not None:
                truncated = (
                    finish_reason == "length"
                    or (completion_tokens is not None and max_tokens is not None and completion_tokens >= max_tokens)
                )
                yield {
                    "type": "token_usage",
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "thoughts_tokens": thoughts_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "truncated": truncated,
                    "max_tokens": max_tokens,
                }

            full_response_parts = []
            if thought_parts:
                full_response_parts.append("<thought>\n" + "".join(thought_parts) + "\n</thought>")
            if content_parts:
                full_response_parts.append("".join(content_parts))

            completed_tools = []
            for tc_info in tool_accum.values():
                try:
                    args = json.loads(tc_info["arguments"]) if tc_info["arguments"] else {}
                except json.JSONDecodeError:
                    args = {}
                completed_tools.append({"name": tc_info["name"], "arguments": args})
            if completed_tools:
                full_response_parts.append("[Tool calls] " + ", ".join(t["name"] for t in completed_tools))

            provider_write_log(
                provider="openai",
                model=self.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                thoughts_tokens=thoughts_tokens,
                cache_read_tokens=cache_read_tokens,
                duration_ms=int((time.perf_counter() - _t0) * 1000),
                tools=tools,
                messages=messages,
                tool_calls_list=completed_tools,
                response_texts=full_response_parts,
            )

        except Exception as exc:
            from ..errors import FallbackTriggered
            if isinstance(exc, FallbackTriggered):
                provider_write_log(
                    provider="openai",
                    model=self.model,
                    prompt_tokens=None,
                    completion_tokens=None,
                    duration_ms=int((time.perf_counter() - _t0) * 1000),
                    tools=tools,
                    messages=messages,
                    tool_calls_list=[],
                    response_texts=["ERROR: stalled or capacity-exhausted — fallback"],
                )
                raise
            logger.error("OpenAI API error: %s", exc, exc_info=True)
            provider_write_log(
                provider="openai",
                model=self.model,
                prompt_tokens=None,
                completion_tokens=None,
                duration_ms=int((time.perf_counter() - _t0) * 1000),
                tools=tools,
                messages=messages,
                tool_calls_list=[],
                response_texts=[f"ERROR: {exc}"],
            )
            yield {"type": "error", "error": str(exc)}


class VLLMProvider(OpenAIProvider):

    _is_vllm: bool = True

    def __init__(
        self,
        model: str = "gemma-4-26b-a4b",
        api_key: str | None = None,
        base_url: str = "http://localhost:8421/v1",
        **kwargs,
    ) -> None:
        resolved_key = api_key or os.getenv("VLLM_API_KEY", "token-abc")
        super().__init__(model=model, api_key=resolved_key, base_url=base_url, **kwargs)


class GroqProvider(OpenAIProvider):
    def __init__(self, model: str = "llama-3.3-70b-versatile", api_key: str | None = None, **kwargs) -> None:
        super().__init__(model=model, api_key=api_key, base_url="https://api.groq.com/openai/v1", **kwargs)


class DeepSeekProvider(OpenAIProvider):

    _is_deepseek: bool = True

    def supports_vision(self) -> bool:
        return self.model.lower() in {
            "deepseek-flash",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
        }

    def __init__(self, model: str = "deepseek-flash", api_key: str | None = None, **kwargs) -> None:
        super().__init__(model=model, api_key=api_key, base_url="https://api.deepseek.com", **kwargs)


class XAIProvider(OpenAIProvider):
    def __init__(self, model: str = "grok-4-1-fast-reasoning", api_key: str | None = None, **kwargs) -> None:
        super().__init__(model=model, api_key=api_key, base_url="https://api.x.ai/v1", **kwargs)


class OpenRouterProvider(OpenAIProvider):
    def __init__(self, model: str = "anthropic/claude-sonnet-4-6", api_key: str | None = None, **kwargs) -> None:
        super().__init__(model=model, api_key=api_key, base_url="https://openrouter.ai/api/v1", **kwargs)


class PerplexityProvider(OpenAIProvider):
    def __init__(self, model: str = "sonar-pro", api_key: str | None = None, **kwargs) -> None:
        super().__init__(model=model, api_key=api_key, base_url="https://api.perplexity.ai", **kwargs)


class MistralProvider(OpenAIProvider):
    def __init__(self, model: str = "mistral-medium-latest", api_key: str | None = None, **kwargs) -> None:
        super().__init__(model=model, api_key=api_key, base_url="https://api.mistral.ai/v1", **kwargs)


class MinimaxProvider(OpenAIProvider):
    def __init__(self, model: str = "MiniMax-M2", api_key: str | None = None, **kwargs) -> None:
        super().__init__(model=model, api_key=api_key, base_url="https://api.minimax.chat/v1", **kwargs)


class AzureOpenAIProvider(BaseLLMProvider):

    def __init__(
        self,
        model: str = "gpt-5-mini",
        api_key: str | None = None,
        azure_endpoint: str | None = None,
        api_version: str = "2024-12-01-preview",
        **kwargs,
    ) -> None:
        super().__init__(model, api_key)
        self.azure_endpoint = azure_endpoint
        self.api_version    = api_version
        try:
            from openai import AsyncAzureOpenAI
            self._client = AsyncAzureOpenAI(
                api_key=api_key,
                azure_endpoint=azure_endpoint or "",
                api_version=api_version,
                **kwargs,
            )
        except ImportError as exc:
            raise ImportError(
                "openai package required. Install: pip install openai>=1.50"
            ) from exc

    def supports_tools(self) -> bool:
        return True

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = True,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        delegate = OpenAIProvider.__new__(OpenAIProvider)
        delegate.model   = self.model
        delegate.api_key = self.api_key
        delegate._client = self._client
        async for chunk in delegate.complete(messages, tools, stream, temperature, max_tokens):
            yield chunk


from . import LLMProvider, register_provider

register_provider(LLMProvider.OPENAI, OpenAIProvider)
register_provider(LLMProvider.AZURE_OPENAI, AzureOpenAIProvider)
register_provider(LLMProvider.VLLM, VLLMProvider)
register_provider(LLMProvider.GROQ, GroqProvider)
register_provider(LLMProvider.DEEPSEEK, DeepSeekProvider)
register_provider(LLMProvider.XAI, XAIProvider)
register_provider(LLMProvider.OPENROUTER, OpenRouterProvider)
register_provider(LLMProvider.PERPLEXITY, PerplexityProvider)
register_provider(LLMProvider.MISTRAL, MistralProvider)
register_provider(LLMProvider.MINIMAX, MinimaxProvider)
