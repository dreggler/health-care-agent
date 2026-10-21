import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any

from ..config import settings
from ..utils import ts_now
from .chat_stream import ReplyDeltaStreamer, result_preview, summarize_tool_args
from .errors import ErrorCategory, FallbackTriggered, classify_error
from .llm import set_llm_log_context
from .tools.base import _tool_role
from .tools.result_manager import truncate_result

_primary_down_until: dict[tuple[str, str], float] = {}

_fallback_providers: dict[tuple[str, str], Any] = {}

logger = logging.getLogger(__name__)

ONE_SHOT_ANALYSIS_TIMEOUT_S = 15 * 60.0

CHAT_ALLOWED_TOOLS = frozenset({"reply_user", "finish_chat", "analyze", "manage"})
SUB_ANALYSIS_BASE_TOOLS = frozenset({"sql", "code", "read_skill"})
TOOL_NOT_AVAILABLE_RESULT = {"success": False, "error": "tool not available in this role"}

QUICK_FAILURE_MESSAGE = "Couldn't check in right now. I'll try again soon."


_background_tasks: set[asyncio.Task] = set()


def spawn_background(coro, *, label: str = "background task") -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("%s failed: %s", label, t.exception())

    task.add_done_callback(_done)
    return task


_META_MARKER_RE = re.compile(
    r"\[(?:Repl\w*|TELEGRAM\s+MESSAGE\s+from\s+\S+)\s+at\s+[^\]]*\]:?\s*",
    re.IGNORECASE,
)


def _strip_meta_markers(text: str) -> str:
    return _META_MARKER_RE.sub("", text).strip()


_RAW_TOOL_CALL_RE = re.compile(
    r"<\|?tool_call\|?>.*?(?:</?tool_call\|?>|$)",
    re.DOTALL,
)


def _contains_raw_tool_call(text: str) -> bool:
    return bool(
        "<tool_call>" in text
        or "<|tool_call>" in text
        or "<tool_call|>" in text
    )


def _strip_raw_tool_calls(text: str) -> str:
    return _RAW_TOOL_CALL_RE.sub("", text).strip()


def _filter_data_evidence(tool_results: list[dict]) -> list[dict]:
    return [
        tr for tr in tool_results
        if tr.get("result", {}).get("success", False)
    ]


def _derive_quick_message(findings: str, max_len: int = 80) -> str:
    for line in (findings or "").splitlines():
        s = line.strip().lstrip("#-* \t").strip()
        if s:
            return s[:max_len].rstrip()
    return "Looking healthy at the moment."


def _hash_tool_calls(tool_calls: list[dict]) -> str:
    sig = json.dumps(
        [(tc.get("name"), tc.get("arguments")) for tc in tool_calls],
        sort_keys=True, default=str,
    )
    return hashlib.md5(sig.encode()).hexdigest()


def _is_escaped(raw: str, i: int) -> bool:
    backslashes = 0
    j = i - 1
    while j >= 0 and raw[j] == '\\':
        backslashes += 1
        j -= 1
    return backslashes % 2 == 1


def _sanitize_json_string(raw: str) -> str:
    out: list[str] = []
    in_str = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '"' and not _is_escaped(raw, i):
            in_str = not in_str
            out.append(ch)
        elif in_str and ch == '\n':
            out.append('\\n')
        elif in_str and ch == '\r':
            out.append('\\r')
        elif in_str and ch == '\t':
            out.append('\\t')
        else:
            out.append(ch)
        i += 1
    return ''.join(out)


def _gemma_normalize_quotes(body: str) -> str:
    MARKER = '<|"|>'
    parts: list[str] = body.split(MARKER)
    out: list[str] = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            out.append('"')
            out.append(part.replace('\\', '\\\\').replace('"', '\\"'))
            out.append('"')
        else:
            out.append(part)
    return "".join(out)


def _parse_xml_tool_calls(text: str) -> list[dict]:
    results: list[dict] = []

    for match in re.finditer(r"<\|tool_call>\s*(.*?)(?:\s*<tool_call\|>|\s*$)", text, re.DOTALL):
        body = match.group(1).strip()
        body = _gemma_normalize_quotes(body)
        gemma_match = re.match(r"call:(\w+)\s*(\{.*\})", body, re.DOTALL)
        if gemma_match:
            fn_name = gemma_match.group(1)
            raw_json = gemma_match.group(2)
            raw_json = _sanitize_json_string(raw_json)
            try:
                args = json.loads(raw_json)
                results.append({"name": fn_name, "arguments": args})
            except json.JSONDecodeError:
                fixed = re.sub(r'(?<=[{,])\s*(\w+)\s*:', r' "\1":', raw_json)
                try:
                    args = json.loads(fixed)
                    results.append({"name": fn_name, "arguments": args})
                except json.JSONDecodeError:
                    pass

    if results:
        return results

    for match in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.DOTALL):
        body = match.group(1).strip()

        if body.startswith("{"):
            try:
                tc_data = json.loads(body)
                if "name" in tc_data:
                    results.append({
                        "name": tc_data["name"],
                        "arguments": tc_data.get("arguments", {}),
                    })
                    continue
            except json.JSONDecodeError:
                pass

        qwen_match = re.search(r"<function=(\S+?)>(.*?)</function>", body, re.DOTALL)
        if qwen_match:
            fn_name = qwen_match.group(1)
            params_text = qwen_match.group(2)
            args: dict[str, Any] = {}
            for pm in re.finditer(
                r"<parameter=(\S+?)>\s*(.*?)\s*</parameter>", params_text, re.DOTALL
            ):
                key = pm.group(1)
                val = pm.group(2)
                try:
                    args[key] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    args[key] = val
            results.append({"name": fn_name, "arguments": args})
            continue

        glm_args = re.findall(
            r"<arg_key>\s*(.*?)\s*</arg_key>\s*<arg_value>\s*(.*?)\s*</arg_value>",
            body,
            re.DOTALL,
        )
        if glm_args:
            fn_name = re.sub(r"<arg_key>.*", "", body, flags=re.DOTALL).strip()
            args = {}
            for key, val in glm_args:
                try:
                    args[key] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    args[key] = val
            if fn_name:
                results.append({"name": fn_name, "arguments": args})
            continue

        generic = re.match(r"(\w+)\s*[\n({]", body)
        if generic:
            fn_name = generic.group(1)
            json_match = re.search(r"\{.*?\}", body, re.DOTALL)
            if json_match:
                try:
                    args = json.loads(json_match.group())
                    results.append({"name": fn_name, "arguments": args})
                    continue
                except json.JSONDecodeError:
                    pass

    return results


def _yield_missing_tool_results(messages: list) -> list:
    last_assistant = next(
        (m for m in reversed(messages) if m["role"] == "assistant"), None
    )
    if not last_assistant:
        return messages
    existing_result_ids: set = set()
    for m in messages:
        if m["role"] == "tool":
            tc_id = m.get("tool_call_id")
            if tc_id:
                existing_result_ids.add(tc_id)
    missing: list[tuple[str, str]] = []
    for tc in last_assistant.get("tool_calls", []):
        tc_id = tc.get("id", "")
        if tc_id and tc_id not in existing_result_ids:
            missing.append((tc_id, (tc.get("function") or {}).get("name", "")))
    if not missing:
        return messages
    for id_, name in missing:
        messages.append(_build_tool_result_msg(
            id_,
            {"success": False, "error": "Tool execution was interrupted."},
            name,
            truncate=False,
        ))
    logger.warning("Injected %d synthetic tool results for orphaned tool_use blocks", len(missing))
    return messages


def _loop_meta(loop: str, agent, chat_id: str) -> dict:
    if loop == "chat":
        return {"chat_id": chat_id}
    if loop == "quick":
        return {"task": "quick_analysis"}
    return {"cycle": agent.cycle_count}


def _provider_key(llm: Any) -> tuple[str, str]:
    return (type(llm).__name__, str(getattr(llm, "model", "") or ""))


def _get_fallback_provider() -> Any:
    key = (settings.FALLBACK_LLM_PROVIDER or "", settings.FALLBACK_LLM_MODEL or "")
    provider = _fallback_providers.get(key)
    if provider is None:
        from . import llm_providers
        provider = llm_providers.create_provider(*key)
        _fallback_providers[key] = provider
    return provider


async def _run_with_provider_fallback(primary: Any, attempt, reset) -> None:
    has_fallback = bool(settings.FALLBACK_LLM_PROVIDER and settings.FALLBACK_LLM_MODEL)
    pkey = _provider_key(primary)
    cooling = has_fallback and time.monotonic() < _primary_down_until.get(pkey, 0.0)

    if not cooling:
        try:
            await attempt(primary)
            return
        except FallbackTriggered as exc:
            if not has_fallback:
                raise
            _primary_down_until[pkey] = time.monotonic() + settings.LLM_FALLBACK_COOLDOWN_SECONDS
            logger.warning(
                "LLM fallback: primary %s/%s gave up (%s) — switching to %s/%s for %.0fs",
                pkey[0], pkey[1], exc.reason or "stalled or capacity-exhausted",
                settings.FALLBACK_LLM_PROVIDER, settings.FALLBACK_LLM_MODEL,
                settings.LLM_FALLBACK_COOLDOWN_SECONDS,
            )
        reset()

    fallback = _get_fallback_provider()
    try:
        await attempt(fallback)
    except asyncio.CancelledError:
        raise
    except Exception as fb_exc:
        if not cooling:
            raise
        logger.warning(
            "LLM fallback failed during primary cooldown (%s) — trying primary %s once",
            fb_exc, pkey[1],
        )
        reset()
        await attempt(primary)
        _primary_down_until.pop(pkey, None)


class _ResilientProvider:

    def __init__(self, primary: Any) -> None:
        self._primary = primary
        self.model = getattr(primary, "model", "")

    async def complete(self, messages, tools=None, stream=True, max_tokens=None, temperature=0.7):
        chunks: list[dict] = []

        async def _attempt(llm: Any) -> None:
            async for chunk in llm.complete(
                messages=messages, tools=tools, stream=stream,
                max_tokens=max_tokens, temperature=temperature,
            ):
                if chunk.get("type") == "error":
                    raise RuntimeError(chunk.get("error", "LLM error"))
                chunks.append(chunk)

        await _run_with_provider_fallback(self._primary, _attempt, chunks.clear)
        for chunk in chunks:
            yield chunk


def _check_cancelled(agent: Any) -> None:
    check = getattr(getattr(agent, "_cancellation", None), "check", None)
    if callable(check):
        check()


def _read_image_b64(path: str) -> str:
    import base64 as _b64
    with open(path, "rb") as f:
        return _b64.b64encode(f.read()).decode()


def _normalize_history(hist: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in hist:
        if not out and m.get("role") != "user":
            continue
        if (
            out
            and out[-1].get("role") == m.get("role")
            and isinstance(out[-1].get("content"), str)
            and isinstance(m.get("content"), str)
        ):
            out[-1] = {**out[-1], "content": out[-1]["content"] + "\n\n" + m["content"]}
            continue
        out.append(m)
    return out


def _trim_history(hist: list[dict], limit: int) -> list[dict]:
    if len(hist) > limit:
        hist = hist[-limit:]
    while hist and hist[0].get("role") != "user":
        hist = hist[1:]
    return hist


async def _clear_reply_stream(
    agent, chat_id: str, reason: str = "rejected", detail: str = "",
) -> None:
    if not getattr(agent, "_reply_streamed", False):
        return
    agent._reply_streamed = False
    event: dict = {
        "type": "chat_reply_delta", "text": "", "reset": True,
        "reason": reason, "chat_id": chat_id,
    }
    if detail:
        event["detail"] = detail[:300]
    await agent._emit(event)


async def _llm_call(
    agent,
    messages: list[dict],
    tools: list[dict],
    *,
    loop: str,
    chat_id: str = "",
    max_tokens: int | None = None,
    temperature: float = 0.7,
    prev_tool_results: list[dict] | None = None,
    timeout: float | None = None,
) -> tuple[str, list[dict], str | None]:
    response_content: list[str] = []
    tool_calls: list[dict] = []
    thought_signature = None

    set_llm_log_context(
        user_id=agent.user_id,
        loop=loop,
        chat_id=chat_id,
        tool_results=prev_tool_results or [],
    )

    streamer: ReplyDeltaStreamer | None = ReplyDeltaStreamer() if loop == "chat" else None
    if loop == "chat":
        agent._reply_streamed = False

    async def _emit_reply_delta(text: str) -> None:
        agent._reply_streamed = True
        await agent._emit({
            "type": "chat_reply_delta", "text": text,
            **_loop_meta(loop, agent, chat_id),
        })

    async def _consume(llm) -> None:
        nonlocal thought_signature, streamer
        if streamer is not None and streamer.emitted:
            streamer = ReplyDeltaStreamer()
            await _clear_reply_stream(agent, chat_id, "retry")
        async for chunk in llm.complete(
            messages=messages,
            tools=tools,
            stream=True,
            max_tokens=max_tokens or settings.AGENT_MAX_TOKENS or None,
            temperature=temperature,
        ):
            ctype = chunk["type"]
            if ctype == "content":
                response_content.append(chunk["content"])
                await agent._emit({
                    "type": "chat_content" if loop == "chat" else "content",
                    "content": chunk["content"],
                    **_loop_meta(loop, agent, chat_id),
                })
            elif ctype == "agent_thinking":
                await agent._emit({
                    "type": "chat_thinking" if loop == "chat" else "agent_thinking",
                    "content": chunk.get("content", ""),
                    **_loop_meta(loop, agent, chat_id),
                })
            elif ctype == "thought_signature":
                thought_signature = chunk["signature"]
            elif ctype == "tool_call":
                tool_calls.append(chunk)
            elif ctype == "tool_call_delta":
                if streamer is not None:
                    try:
                        snap = streamer.feed(chunk)
                    except Exception:
                        snap = None
                    if snap is not None:
                        await _emit_reply_delta(snap)
            elif ctype == "token_usage":
                prompt = chunk.get("prompt_tokens") or 0
                completion = chunk.get("completion_tokens") or chunk.get("response_tokens") or 0
                thoughts = chunk.get("thoughts_tokens") or 0
                cache_read = chunk.get("cache_read_tokens") or 0
                cache_creation = chunk.get("cache_creation_tokens") or 0
                agent.cumulative_tokens["prompt_tokens"] += prompt
                agent.cumulative_tokens["completion_tokens"] += completion
                agent.cumulative_tokens["thoughts_tokens"] += thoughts
                agent.cumulative_tokens.setdefault("cache_read_tokens", 0)
                agent.cumulative_tokens.setdefault("cache_creation_tokens", 0)
                agent.cumulative_tokens["cache_read_tokens"] += cache_read
                agent.cumulative_tokens["cache_creation_tokens"] += cache_creation
                await agent._emit({
                    "type": "token_usage",
                    "prompt_tokens": chunk.get("prompt_tokens"),
                    "completion_tokens": chunk.get("completion_tokens"),
                    "thoughts_tokens": chunk.get("thoughts_tokens"),
                    "response_tokens": chunk.get("response_tokens"),
                    "cache_read_tokens": chunk.get("cache_read_tokens"),
                    "cache_creation_tokens": chunk.get("cache_creation_tokens"),
                    **_loop_meta(loop, agent, chat_id),
                })
            elif ctype == "error":
                await agent._emit({"type": "error", "error": chunk["error"], **_loop_meta(loop, agent, chat_id)})
                raise RuntimeError(chunk["error"])

    def _reset() -> None:
        response_content.clear()
        tool_calls.clear()

    async def _run_with_fallback() -> None:
        await _run_with_provider_fallback(agent.llm, _consume, _reset)

    if timeout is not None:
        await asyncio.wait_for(_run_with_fallback(), timeout=timeout)
    else:
        await _run_with_fallback()

    if streamer is not None:
        final_snap = streamer.finish()
        if final_snap is not None:
            await _emit_reply_delta(final_snap)

    text = "".join(response_content)

    valid = [tc for tc in tool_calls if tc.get("name")]
    if not valid and ("<tool_call>" in text or "<|tool_call>" in text):
        valid = _parse_xml_tool_calls(text)

    for tc in valid:
        if not tc.get("id"):
            tc["id"] = f"call_{uuid.uuid4().hex[:24]}"

    return text, valid, thought_signature


def _parse_arguments(arguments: Any) -> dict:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return {}
    return arguments if isinstance(arguments, dict) else {}


def _build_assistant_msg(text: str, tool_calls: list[dict], signature: str | None = None) -> dict:
    msg: dict[str, Any] = {"role": "assistant", "content": text or None}
    if signature:
        msg["signature"] = signature
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": (
                        json.dumps(tc["arguments"], ensure_ascii=False)
                        if isinstance(tc["arguments"], dict)
                        else tc.get("arguments", "{}")
                    ),
                },
            }
            for tc in tool_calls
        ]
    return msg


_ORCHESTRATOR_HIDDEN_KEYS = ("evidence", "report_args")


def _build_tool_result_msg(
    tool_call_id: str, result: dict, tool_name: str = "", *, truncate: bool = True,
) -> dict:
    if truncate and isinstance(result, dict):
        if tool_name in ("analyze", "manage"):
            result = {k: v for k, v in result.items() if k not in _ORCHESTRATOR_HIDDEN_KEYS}
        result = truncate_result(result, tool_name)
    content = json.dumps(result, ensure_ascii=False, default=str)
    msg: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
    }
    if tool_name:
        msg["_tool_name"] = tool_name
    return msg


def _find_turn_groups(
    messages: list[dict], start_idx: int,
) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    for msg in messages[start_idx:]:
        if msg["role"] == "assistant":
            if current:
                groups.append(current)
            current = [msg]
        elif msg["role"] == "tool" and current:
            current.append(msg)
        else:
            if current:
                current.append(msg)
    if current:
        groups.append(current)
    return groups


def _extract_turn_digest(group: list[dict], turn_num: int) -> str:
    assistant_msg = group[0]
    tool_msgs = group[1:]

    parts: list[str] = []
    tool_calls = assistant_msg.get("tool_calls", [])
    for tc in tool_calls:
        fn = tc.get("function", {})
        name = fn.get("name", "?")
        raw_args = fn.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
        else:
            args = raw_args

        if name == "sql":
            parts.append(f"sql({args.get('query', '')[:120]})")
        elif name == "code":
            first_line = args.get("code", "").strip().split("\n")[0][:80]
            parts.append(f"code({first_line})")
        else:
            parts.append(f"{name}()")

    result_parts: list[str] = []
    for tm in tool_msgs:
        try:
            result = json.loads(tm.get("content", "{}"))
            if result.get("success"):
                if "markdown" in result:
                    md_lines = result["markdown"].strip().split("\n")
                    keep = md_lines[:7] if len(md_lines) <= 9 else md_lines[:5]
                    rc = result.get("row_count", "?")
                    table = "\n".join(keep)
                    if len(md_lines) > 9:
                        table += f"\n... ({rc} rows total)"
                    result_parts.append(f"{rc} rows:\n{table}")
                elif "output" in result:
                    result_parts.append(f"output: {str(result['output'])[:200]}")
                else:
                    result_parts.append("ok")
            else:
                result_parts.append(f"ERROR: {result.get('error', '?')[:80]}")
        except (json.JSONDecodeError, TypeError):
            pass

    calls_str = ", ".join(parts) if parts else "(text only)"
    results_str = " | ".join(result_parts) if result_parts else ""

    line = f"Turn {turn_num}: {calls_str}"
    if results_str:
        line += f"\n  → {results_str}"
    return line


async def _summarize_turns_with_llm(
    turn_groups: list[list[dict]],
    start_turn: int,
    llm_provider,
) -> str:
    digests = [
        _extract_turn_digest(g, start_turn + i)
        for i, g in enumerate(turn_groups)
    ]
    digest_text = "\n".join(digests)

    end_turn = start_turn + len(turn_groups) - 1
    label = f"Steps {start_turn}-{end_turn}"

    fallback = f"{label}: " + " | ".join(
        _extract_turn_digest(g, start_turn + i).split("\n")[0]
        for i, g in enumerate(turn_groups)
    )
    if len(fallback) > 500:
        fallback = fallback[:497] + "..."

    try:
        prompt_msgs = [{
            "role": "user",
            "content": (
                "Summarize these analysis steps in 1-2 sentences. "
                "Keep ALL specific numbers and key findings.\n\n"
                f"{digest_text}"
            ),
        }]
        set_llm_log_context(loop="context_compression")
        parts: list[str] = []
        async for chunk in llm_provider.complete(
            messages=prompt_msgs,
            tools=None,
            stream=True,
            max_tokens=150,
            temperature=0.0,
        ):
            if chunk.get("type") == "content":
                parts.append(chunk["content"])

        summary = "".join(parts).strip()
        if summary:
            return f"{label}: {summary}"
    except Exception as exc:
        logger.debug("LLM summarization failed, using structural fallback: %s", exc)

    return fallback


def _extract_key_findings(turn_groups: list[list[dict]]) -> str:
    _FINDING_KEYWORDS = (
        "average", "mean", "total", "highest", "lowest",
        "increase", "decrease", "normal", "abnormal",
        "concern", "recommend", "pattern", "trend",
        "bpm", "steps", "hours", "minutes", "mg/dl",
        "percent", "%", "score", "index",
    )
    findings: list[str] = []

    for group in turn_groups:
        for msg in group:
            if msg.get("role") == "tool":
                try:
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        result = json.loads(content)
                    else:
                        result = content
                    if isinstance(result, dict) and result.get("success"):
                        if "row_count" in result and "columns" in result:
                            cols = result.get("columns", [])
                            rc = result.get("row_count", 0)
                            findings.append(f"SQL: {rc} rows, cols={cols[:5]}")
                        elif "output" in result:
                            out = str(result["output"])
                            if out and len(out) < 300:
                                findings.append(f"Code: {out[:200]}")
                except (json.JSONDecodeError, TypeError):
                    pass

            elif msg.get("role") == "assistant":
                content = str(msg.get("content", ""))
                for line in content.split("\n"):
                    line_lower = line.lower()
                    if any(kw in line_lower for kw in _FINDING_KEYWORDS):
                        stripped = line.strip()[:150]
                        if stripped:
                            findings.append(f"Finding: {stripped}")

    unique = list(dict.fromkeys(findings))[:10]
    return "\n".join(unique)


async def _compress_context_if_needed(
    messages: list[dict],
    preamble_size: int,
    window_size: int,
    original_user_content: str,
    llm_provider,
    accumulated_summary: str,
) -> tuple[list[dict], str]:
    batch_size = max(1, window_size // 4)
    groups = _find_turn_groups(messages, preamble_size)

    if len(groups) <= window_size + batch_size:
        return messages, accumulated_summary

    to_compress = groups[:batch_size]
    to_keep = groups[batch_size:]

    key_findings = _extract_key_findings(to_compress)

    prev_batches = len(
        re.findall(r"^Steps \d+-\d+:", accumulated_summary, re.MULTILINE)
    )
    start_turn = prev_batches * batch_size + 1

    summary_line = await _summarize_turns_with_llm(
        to_compress, start_turn, llm_provider,
    )

    if key_findings:
        summary_line += f"\nKey findings: {key_findings}"

    new_summary = (
        f"{accumulated_summary}\n{summary_line}"
        if accumulated_summary
        else summary_line
    )

    logger.info(
        "Context window compressed: %d turn groups -> %d kept, %d summarized",
        len(groups), len(to_keep), batch_size,
    )

    new_messages = list(messages[:preamble_size])

    user_idx = preamble_size - 1
    new_messages[user_idx] = {
        **new_messages[user_idx],
        "content": (
            original_user_content
            + "\n\n--- Previous analysis context (auto-summarized) ---\n"
            + new_summary
            + "\n---"
        ),
    }

    for group in to_keep:
        new_messages.extend(group)

    return new_messages, new_summary


async def _push_report_programmatic(
    agent: Any, push_args: dict, evidence: list[dict], timeout: float = 60.0,
) -> dict:
    task = asyncio.ensure_future(
        agent._execute_tool("push_report", push_args, evidence_trail=evidence)
    )
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("push_report still running after %.0fs — letting it finish in background", timeout)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return {"success": False, "error": f"push_report did not finish within {timeout:.0f}s"}


async def _run_sub_analysis_loop(
    self,
    goal: str,
    *,
    chat_id: str | None,
    source: str,
    max_turns: int,
    allowed_tools: set | None,
    extra_tools: set | None,
    system_prompt: str | None,
) -> dict:
    if chat_id is not None:
        try:
            reply_tool = self.tool_registry.get_tool("reply_user")
            registry = getattr(reply_tool, "_gateway_registry", None) if reply_tool else None
            if registry is not None:
                from ..messaging.base import MessageChannel as _MC
                tg_gw = registry.get(_MC.TELEGRAM)
                sender = getattr(tg_gw, "sender", None) if tg_gw is not None else None
                if sender is not None and hasattr(sender, "send_chat_action"):
                    await sender.send_chat_action(chat_id, "typing")
        except Exception:
            pass

    _code_tool = self.tool_registry.get_tool("code")
    if _code_tool is not None and hasattr(_code_tool, "refresh_df_async"):
        await _code_tool.refresh_df_async()

    if system_prompt is None:
        system_prompt = await asyncio.to_thread(self.build_sub_analysis_prompt)

    from ..utils import language_directive, local_tz_line
    preamble = f"[{local_tz_line()}]"
    if source in ("analysis", "quick", "plan"):
        try:
            sample = await asyncio.to_thread(self._chat_memory().recent_user_text)
            preamble += f"\n[{language_directive(sample, settings.DEFAULT_USER_LANGUAGE)}]"
        except Exception as exc:
            logger.debug("language inference skipped: %s", exc)
        preamble += (
            "\n[Honesty: if the data needed for this report is missing, "
            "empty, or insufficient for the requested period, state that "
            "honestly in your findings. Do NOT invent numbers and do NOT "
            "reuse or rephrase data from a previous report to fill gaps. "
            "Absence of data is itself a valid, reportable finding.]"
        )
    original_user_content = f"{preamble}\n{goal}"
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": original_user_content},
    ]
    accumulated_summary = ""

    sub_tools = self._get_sub_analysis_tool_definitions(extra_tools=extra_tools)
    _executable_tools = set(SUB_ANALYSIS_BASE_TOOLS)
    if extra_tools:
        _executable_tools |= set(extra_tools)
    if allowed_tools is not None:
        _executable_tools &= set(allowed_tools)
        sub_tools = [
            t for t in sub_tools
            if (t.get("function", {}) if isinstance(t, dict) else {}).get("name") in _executable_tools
        ]
    all_evidence: list[dict] = []
    charts: list[str] = []
    findings = ""
    prev_tool_results: list[dict] = []
    prev_call_sig = ""
    dup_count = 0

    evt_call = f"{source}_tool_call"
    evt_result = f"{source}_tool_result"

    def _evt_payload(base: dict) -> dict:
        if chat_id is not None:
            base["chat_id"] = chat_id
        base["source"] = source
        if source == "chat":
            base["parent"] = "analyze"
        return base

    def _failure(exc: Exception) -> dict:
        err = str(exc) or type(exc).__name__
        logger.error("Sub-analysis (%s) failed: %s", source, err)
        return {
            "success": False,
            "error": err[:500],
            "findings": "",
            "charts": charts,
            "evidence": _filter_data_evidence(all_evidence),
        }

    for _turn in range(1, max_turns + 1):
        _check_cancelled(self)
        try:
            text, tool_calls, sig = await _llm_call(
                self, messages, sub_tools,
                loop=source, chat_id=chat_id,
                prev_tool_results=prev_tool_results,
            )
        except Exception as e:
            agent_error = classify_error(e, "sub_analysis_llm")
            logger.error("Sub-analysis LLM error (%s): %s", agent_error.category.value, e)
            if agent_error.category == ErrorCategory.CONTEXT_OVERFLOW:
                from .context_manager import ContextManager
                before = len(messages)
                messages, _ = ContextManager().emergency_truncate(messages, 2)
                if len(messages) >= before:
                    return _failure(e)
                try:
                    text, tool_calls, sig = await _llm_call(
                        self, messages, sub_tools,
                        loop=source, chat_id=chat_id,
                        prev_tool_results=prev_tool_results,
                    )
                except Exception as e2:
                    return _failure(e2)
            else:
                return _failure(e)

        if not tool_calls:
            findings = text
            break

        call_sig = _hash_tool_calls(tool_calls)
        if call_sig == prev_call_sig:
            dup_count += 1
            if dup_count >= 2:
                logger.warning("Sub-analysis (%s): 3 identical rounds, breaking", source)
                findings = text or "Analysis stalled."
                break
        else:
            dup_count = 0
        prev_call_sig = call_sig

        messages.append(_build_assistant_msg(text, tool_calls, sig))
        tool_results: list[dict] = []

        for tc in tool_calls:
            tool_name = tc.get("name")
            tc_id = tc.get("id", "")
            arguments = _parse_arguments(tc.get("arguments", {}))
            if tool_name not in _executable_tools:
                logger.warning(
                    "Sub-analysis (%s): rejected tool %r (not available in this role)",
                    source, tool_name,
                )
                tool_results.append({
                    "id": tc_id, "tool": tool_name, "arguments": arguments,
                    "result": dict(TOOL_NOT_AVAILABLE_RESULT),
                })
                continue

            await self._emit(_evt_payload({
                "type": evt_call, "tool": tool_name,
                "arguments": arguments,
                "call_id": tc_id, "status": "running",
                "summary": summarize_tool_args(tool_name, arguments),
            }))

            evidence_kw: dict = {}
            if tool_name == "push_report":
                evidence_kw["evidence_trail"] = _filter_data_evidence(all_evidence + tool_results)
                arguments["source"] = (
                    "quick_analysis" if source == "quick" else "scheduled_analysis"
                )

            try:
                result = await asyncio.wait_for(
                    self._execute_tool(tool_name, arguments, **evidence_kw),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                result = {"success": False, "error": "Tool timed out."}
            await self._emit(_evt_payload({
                "type": evt_result, "tool": tool_name,
                "success": result.get("success", False),
                "result": result,
                "call_id": tc_id,
                "status": "ok" if result.get("success", False) else "error",
                "result_preview": result_preview(result),
            }))
            tool_results.append({
                "id": tc_id, "tool": tool_name,
                "arguments": arguments, "result": result,
            })

            if tool_name == "code" and result.get("success"):
                code_text = arguments.get("code", "")
                for m in re.finditer(
                    r"savefig\(['\"](/tmp/[^'\"]+\.png)['\"]", code_text,
                ):
                    charts.append(m.group(1))

            if tool_name == "push_report" and result.get("success"):
                all_evidence.extend(tool_results)
                return {
                    "success": True,
                    "findings": text or "",
                    "charts": charts,
                    "evidence": _filter_data_evidence(all_evidence),
                    "report_pushed": True,
                    "report_id": result.get("report_id"),
                    "report_args": arguments,
                }

        all_evidence.extend(tool_results)
        prev_tool_results = tool_results
        for tr in tool_results:
            messages.append(
                _build_tool_result_msg(tr["id"], tr["result"], tr["tool"])
            )
        _yield_missing_tool_results(messages)

        try:
            messages, accumulated_summary = await _compress_context_if_needed(
                messages, 2, self.context_window_size,
                original_user_content, _ResilientProvider(self.llm), accumulated_summary,
            )
        except Exception as exc:
            logger.warning("Context compression error (sub_analysis): %s", exc)

        findings = text

    if not findings:
        findings = "Analysis completed but no findings were generated."

    return {
        "success": True,
        "findings": findings,
        "charts": charts,
        "evidence": _filter_data_evidence(all_evidence),
    }


async def _run_chat_manage_loop(self, goal: str, chat_id: str) -> dict:
    system_prompt = await asyncio.to_thread(self.build_sub_manage_prompt)

    from ..utils import local_tz_line
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"[{local_tz_line()}]\n{goal}"},
    ]

    sub_tools = self._get_sub_manage_tool_definitions()
    allowed_manage_tools = {
        (t.get("function", {}) if isinstance(t, dict) else {}).get("name")
        for t in sub_tools
    }

    all_evidence: list[dict] = []
    actions_taken: list[str] = []
    result_text = ""
    prev_tool_results: list[dict] = []
    prev_call_sig = ""
    dup_count = 0
    max_sub_turns = 8

    def _failure(exc: Exception) -> dict:
        err = str(exc) or type(exc).__name__
        logger.error("Sub-manage failed: %s", err)
        return {
            "success": False,
            "error": err[:500],
            "result": f"Management error: {err[:300]}",
            "actions_taken": actions_taken,
            "evidence": all_evidence,
        }

    for _turn in range(1, max_sub_turns + 1):
        _check_cancelled(self)
        try:
            text, tool_calls, sig = await _llm_call(
                self, messages, sub_tools,
                loop="chat", chat_id=chat_id,
                prev_tool_results=prev_tool_results,
            )
        except Exception as e:
            agent_error = classify_error(e, "sub_manage_llm")
            logger.error("Sub-manage LLM error (%s): %s", agent_error.category.value, e)
            if agent_error.category == ErrorCategory.CONTEXT_OVERFLOW:
                from .context_manager import ContextManager
                before = len(messages)
                messages, _ = ContextManager().emergency_truncate(messages, 2)
                if len(messages) >= before:
                    return _failure(e)
                try:
                    text, tool_calls, sig = await _llm_call(
                        self, messages, sub_tools,
                        loop="chat", chat_id=chat_id,
                        prev_tool_results=prev_tool_results,
                    )
                except Exception as e2:
                    return _failure(e2)
            else:
                return _failure(e)

        if not tool_calls:
            result_text = text
            break

        call_sig = _hash_tool_calls(tool_calls)
        if call_sig == prev_call_sig:
            dup_count += 1
            if dup_count >= 2:
                logger.warning("Sub-manage: 3 identical rounds, breaking")
                result_text = text or "Management operation stalled."
                break
        else:
            dup_count = 0
        prev_call_sig = call_sig

        messages.append(_build_assistant_msg(text, tool_calls, sig))
        tool_results: list[dict] = []

        for tc in tool_calls:
            tool_name = tc.get("name")
            tc_id = tc.get("id", "")
            arguments = _parse_arguments(tc.get("arguments", {}))
            if tool_name not in allowed_manage_tools:
                logger.warning(
                    "Sub-manage: rejected tool %r (not available in this role)", tool_name,
                )
                tool_results.append({
                    "id": tc_id, "tool": tool_name, "arguments": arguments,
                    "result": dict(TOOL_NOT_AVAILABLE_RESULT),
                })
                continue

            await self._emit({
                "type": "chat_tool_call", "tool": tool_name,
                "arguments": arguments, "chat_id": chat_id,
                "call_id": tc_id, "status": "running", "parent": "manage",
                "summary": summarize_tool_args(tool_name, arguments),
            })
            try:
                result = await asyncio.wait_for(
                    self._execute_tool(tool_name, arguments),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                result = {"success": False, "error": "Tool timed out."}
            await self._emit({
                "type": "chat_tool_result", "tool": tool_name,
                "success": result.get("success", False),
                "result": result, "chat_id": chat_id,
                "call_id": tc_id, "parent": "manage",
                "status": "ok" if result.get("success", False) else "error",
                "result_preview": result_preview(result),
            })
            tool_results.append({
                "id": tc_id, "tool": tool_name,
                "arguments": arguments, "result": result,
            })
            if result.get("success"):
                actions_taken.append(f"{tool_name}: {str(arguments)[:120]}")

        all_evidence.extend(tool_results)
        prev_tool_results = tool_results
        for tr in tool_results:
            messages.append(
                _build_tool_result_msg(tr["id"], tr["result"], tr["tool"])
            )
        _yield_missing_tool_results(messages)
        result_text = text

    if not result_text:
        result_text = "Management operation completed."

    return {
        "success": True,
        "result": result_text,
        "actions_taken": actions_taken,
        "evidence": all_evidence,
    }


class AgentLoopsMixin:


    async def _run_one_shot_analysis(self, goal: str | None) -> None:
        self.cycle_count += 1
        self._set_state("thinking", loop="analysis")

        try:
            latest_data = await asyncio.to_thread(
                self.data_store.query,
                "SELECT MAX(timestamp) as ts FROM samples",
            )
            current_sim_time = None
            if not latest_data.empty and "ts" in latest_data.columns and latest_data.iloc[0]["ts"]:
                current_sim_time = latest_data.iloc[0]["ts"]
            self.current_simulation_timestamp = current_sim_time
        except Exception:
            current_sim_time = None

        await self._emit({
            "type": "cycle_start",
            "cycle": self.cycle_count,
            "timestamp": ts_now(),
            "simulation_time": current_sim_time,
            "goal": goal,
        })

        sub_goal = goal or (
            "Run an open-ended health analysis. Query the data, find "
            "something meaningful to report, and publish the report."
        )
        if getattr(goal, "deadline_passed", None) is not None:
            try:
                from .scheduled_freshness import build_run_context, parse_sample_ts
                ctx = build_run_context(
                    parse_sample_ts(current_sim_time),
                    deadline_passed=bool(goal.deadline_passed),
                    waited_min=float(getattr(goal, "waited_min", 0.0)),
                )
                sub_goal = f"{ctx}\n\n{sub_goal}"
            except Exception as exc:
                logger.debug("run-context line skipped: %s", exc)
        try:
            sub_result = await asyncio.wait_for(
                self._run_sub_analysis(
                    sub_goal,
                    chat_id=None,
                    source="analysis",
                    max_turns=self.max_turns,
                    extra_tools={"push_report"},
                ),
                timeout=ONE_SHOT_ANALYSIS_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Autonomous analysis exceeded %.0fs — abandoned", ONE_SHOT_ANALYSIS_TIMEOUT_S,
            )
            sub_result = {
                "success": False,
                "error": f"analysis timed out after {ONE_SHOT_ANALYSIS_TIMEOUT_S:.0f}s",
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Autonomous analysis sub_analysis error: %s", exc, exc_info=True)
            sub_result = {"success": False, "error": str(exc)}

        findings = sub_result.get("findings", "") or ""
        evidence = _filter_data_evidence(sub_result.get("evidence", []))

        report_published = False
        shared_text = findings
        if not sub_result.get("success", True):
            err = sub_result.get("error") or "analysis failed"
            logger.error("Autonomous analysis failed — nothing published: %s", err)
            await self._emit({
                "type": "error",
                "error": f"Scheduled analysis failed: {err}",
                "cycle": self.cycle_count,
                "source": "analysis",
            })
        elif sub_result.get("report_pushed"):
            report_published = True
            args = sub_result.get("report_args") or {}
            shared_text = args.get("im_digest") or args.get("content") or findings
            self.last_sleep_time = time.time()
            await self._emit({
                "type": "report_pushed",
                "cycle": self.cycle_count,
                "report_id": sub_result.get("report_id"),
                "timestamp": ts_now(),
            })
        else:
            logger.info("LLM did not call push_report — using programmatic fallback")
            title = (goal[:80] if goal else f"Analysis Cycle {self.cycle_count}")
            clean_findings = (
                _strip_raw_tool_calls(findings) if _contains_raw_tool_call(findings)
                else findings.strip()
            )
            if not clean_findings:
                clean_findings = (
                    f"Analysis completed but no detailed findings were generated.\n\n"
                    f"Goal: {goal or 'general analysis'}"
                )
            shared_text = clean_findings

            push_args = {
                "title": title,
                "content": clean_findings,
                "im_digest": clean_findings,
                "time_range_start": current_sim_time or ts_now(),
                "time_range_end": current_sim_time or ts_now(),
                "alert_level": "info",
            }
            try:
                push_result = await _push_report_programmatic(self, push_args, evidence)
                if push_result.get("success"):
                    report_published = True
                    self.last_sleep_time = time.time()
                    await self._emit({
                        "type": "report_pushed",
                        "cycle": self.cycle_count,
                        "report_id": push_result.get("report_id"),
                        "timestamp": ts_now(),
                    })
            except Exception as exc:
                logger.warning("Autonomous push_report fallback failed: %s", exc)

        if report_published and shared_text:
            bot_msg = {"role": "assistant", "content": shared_text}
            reply_tool = self.tool_registry.get_tool("reply_user")
            gw_registry = getattr(reply_tool, "_gateway_registry", None) if reply_tool else None
            if gw_registry is not None:
                for gw in gw_registry.all():
                    default_chat_id = getattr(gw, "default_chat_id", None)
                    if not default_chat_id:
                        continue
                    key = f"{gw.channel.value}:{default_chat_id}"
                    if key not in self._chat_histories:
                        self._chat_histories[key] = []
            for key, hist in list(self._chat_histories.items()):
                self._chat_histories[key] = _trim_history(
                    [*hist, bot_msg], self._max_chat_history,
                ) if hist else [bot_msg]

        await self._emit({
            "type": "cycle_end",
            "cycle": self.cycle_count,
            "timestamp": ts_now(),
        })

        self.last_analysis_complete_time = ts_now()
        self.cycle_messages = []
        self._set_state("idle", loop="analysis")
        self._save_state()


    async def trigger_plan_redesign(self) -> bool:
        if getattr(self, "_plan_running", False):
            return False
        self._plan_running = True
        try:
            survey = await asyncio.to_thread(self._chat_memory().get_pending_survey)
        except Exception:
            self._plan_running = False
            raise
        if not survey:
            self._plan_running = False
            return False
        spawn_background(
            self._run_plan_designer_guarded(survey), label="plan designer",
        )
        logger.info("Plan redesign triggered on demand for %s", self.user_id)
        return True

    async def _run_plan_designer_guarded(self, survey: dict) -> None:
        try:
            await self._run_plan_designer(survey)
        finally:
            self._plan_running = False

    async def _run_plan_designer(self, survey: dict) -> None:
        survey_id = survey.get("id")
        goals = survey.get("goals") or []
        answers = survey.get("answers") or {}
        goal = (
            "A new user just finished onboarding CareAgent and told us — via a goal "
            "survey, with no conversation yet — what they want to achieve. "
            "Design a concrete, personalised health plan and SET IT UP for them: "
            "create the relevant scheduled_tasks, record the plan in memory, and "
            "publish a warm plan report introducing it.\n\n"
            f"Selected goals: {json.dumps(goals, ensure_ascii=False)}\n"
            f"Other survey answers: {json.dumps(answers, ensure_ascii=False)}"
        )
        try:
            system_prompt = await asyncio.to_thread(self.build_plan_designer_prompt)
            result = await self._run_sub_analysis(
                goal,
                chat_id=None,
                source="plan",
                max_turns=self.max_turns,
                extra_tools={"push_report", "update_md"},
                system_prompt=system_prompt,
            )
            if not result.get("success", True):
                logger.error(
                    "Plan designer for %s failed (%s) — survey %s stays pending",
                    self.user_id, result.get("error", "unknown error"), survey_id,
                )
                return
            if not result.get("report_pushed"):
                logger.warning(
                    "Plan designer for %s finished without publishing a report",
                    self.user_id,
                )
            if survey_id is not None:
                await asyncio.to_thread(self._chat_memory().mark_survey_planned, survey_id)
            logger.info("Plan designer completed for %s (survey %s)", self.user_id, survey_id)
        except Exception as exc:
            logger.error("Plan designer failed for %s: %s", self.user_id, exc, exc_info=True)


    async def run_quick_analysis(self) -> dict[str, str]:
        lock = getattr(self, "_quick_lock", None)
        if lock is None:
            lock = self._quick_lock = asyncio.Lock()
        async with lock:
            self._set_state("quick_analysis", loop="analysis")
            await self._emit({"type": "quick_analysis_start", "timestamp": ts_now()})
            final_state = "neutral"
            try:
                result = await self._run_quick_analysis_inner()
                final_state = (result or {}).get("state") or "neutral"
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Quick analysis interrupted: %s", type(exc).__name__)
                return {"state": "neutral", "message": "Analysis interrupted. Try again later."}
            finally:
                self._set_state("idle", loop="analysis")
                await self._emit({
                    "type": "quick_analysis_complete",
                    "state": final_state,
                    "timestamp": ts_now(),
                })

    async def _run_quick_analysis_inner(self) -> dict[str, str]:
        from .prompt_loader import load_prompt

        goal = await asyncio.to_thread(load_prompt, "quick_goal.md")

        try:
            sub_result = await asyncio.wait_for(
                self._run_sub_analysis(
                    goal,
                    chat_id=None,
                    source="quick",
                    max_turns=8,
                    allowed_tools={"sql", "push_report"},
                    extra_tools={"push_report"},
                ),
                timeout=300.0,
            )
        except asyncio.TimeoutError:
            return {
                "state": "neutral",
                "message": "Analysis took too long. Try again later.",
            }
        except Exception as exc:
            logger.error("Quick analysis sub_analysis error: %s", exc)
            return {"state": "neutral", "message": "Analysis error. Try again later."}

        if not sub_result.get("success", True):
            logger.warning("Quick analysis failed: %s", sub_result.get("error", "unknown error"))
            return {"state": "neutral", "message": QUICK_FAILURE_MESSAGE}

        result_state = "relaxed"
        findings = sub_result.get("findings", "") or ""
        result_message = _derive_quick_message(findings)

        if sub_result.get("report_pushed"):
            report_args = sub_result.get("report_args", {})
            meta = report_args.get("metadata")
            if isinstance(meta, dict):
                result_state = meta.get("state", result_state) or result_state
                result_message = meta.get("message", result_message) or result_message

            await self._emit({
                "type": "report_pushed",
                "task": "quick_analysis",
                "report_id": sub_result.get("report_id"),
                "timestamp": ts_now(),
            })
        else:
            evidence = _filter_data_evidence(sub_result.get("evidence", []))
            now_iso = ts_now()
            try:
                push_args = {
                    "title": "Quick Health Check",
                    "content": findings or result_message,
                    "im_digest": (findings[:200] if findings else result_message),
                    "time_range_start": now_iso,
                    "time_range_end": now_iso,
                    "alert_level": "info",
                    "source": "quick_analysis",
                    "metadata": {"state": result_state, "message": result_message},
                }
                push_result = await _push_report_programmatic(self, push_args, evidence)
                if push_result.get("success"):
                    await self._emit({
                        "type": "report_pushed",
                        "task": "quick_analysis",
                        "report_id": push_result.get("report_id"),
                        "timestamp": ts_now(),
                    })
            except Exception as e:
                logger.warning("Quick analysis push_report fallback failed: %s", e)

        return {"state": result_state, "message": result_message}


    async def _run_sub_analysis(
        self,
        goal: str,
        *,
        chat_id: str | None = None,
        source: str = "chat",
        max_turns: int = 15,
        allowed_tools: set | None = None,
        extra_tools: set | None = None,
        system_prompt: str | None = None,
    ) -> dict:
        token = _tool_role.set("plan" if source == "plan" else "analysis")
        try:
            return await _run_sub_analysis_loop(
                self, goal, chat_id=chat_id, source=source, max_turns=max_turns,
                allowed_tools=allowed_tools, extra_tools=extra_tools,
                system_prompt=system_prompt,
            )
        finally:
            _tool_role.reset(token)


    async def _run_chat_manage(self, goal: str, chat_id: str) -> dict:
        token = _tool_role.set("manage")
        try:
            return await _run_chat_manage_loop(self, goal, chat_id)
        finally:
            _tool_role.reset(token)


    async def _handle_chat_message(self, envelope: Any) -> None:
        sender = envelope.sender_id or "user"
        chat_id = envelope.chat_id or sender
        channel = envelope.channel.value if envelope.channel is not None else "chat"
        history_key = f"{channel}:{chat_id}"
        from .chat_threads import envelope_thread_id
        thread_id = envelope_thread_id(envelope, getattr(self, "user_id", "LiveUser"))
        self._chat_thread_id = thread_id
        self._chat_run_id = uuid.uuid4().hex

        if (envelope.content or "").strip().lower() in ("/clear", "/reset"):
            self._chat_histories.pop(history_key, None)
            self._save_state()
            try:
                await self._chat_memory().clear_chat_history(history_key)
            except Exception as e:
                logger.debug("clear_chat_history failed: %s", e)
            await self._emit({"type": "chat_cleared", "chat_id": chat_id})
            self._set_state("chat_complete", loop="chat")
            return

        self.user_messages_received += 1

        self._set_state("chat_processing", loop="chat")
        await self._emit({
            "type": "user_message",
            "sender": sender,
            "content": envelope.content,
            "chat_id": chat_id,
            "channel": channel,
        })

        system_prompt = await asyncio.to_thread(
            self._get_system_prompt, mode="chat"
        )
        history = self._chat_histories.get(history_key, [])
        envelope_ts = envelope.timestamp
        if envelope_ts is None or envelope_ts.year < 2000:
            from datetime import datetime as _dt
            from datetime import timezone as _tz
            envelope_ts = _dt.now(_tz.utc)
        ts_str = self._format_message_timestamp(envelope_ts)
        from ..utils import local_tz_line, now_local
        tz_line = local_tz_line()
        now_str = now_local().strftime("%Y-%m-%d %H:%M")
        user_text = (
            f"[{tz_line}]\n"
            f"[Current local time: {now_str}]\n"
            f"[{channel.upper()} MESSAGE from {sender} at {ts_str}]: {envelope.content}"
        )
        image_atts = [
            a for a in (getattr(envelope, "attachments", None) or [])
            if a.get("type") == "image" and a.get("path")
        ]
        user_msg_for_llm: dict
        if image_atts and self.llm.supports_vision():
            blocks: list[dict] = [{"type": "text", "text": user_text}]
            for att in image_atts:
                try:
                    b64 = await asyncio.to_thread(_read_image_b64, att["path"])
                    mime = att.get("mime", "image/jpeg")
                    blocks.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
                except Exception as e:
                    logger.warning("Could not read inbound image %s: %s", att.get("path"), e)
            user_msg_for_llm = {"role": "user", "content": blocks}
        elif image_atts:
            user_msg_for_llm = {
                "role": "user",
                "content": user_text + "\n[The user attached an image, but the current "
                                       "model cannot view images.]",
            }
        else:
            user_msg_for_llm = {"role": "user", "content": user_text}
        user_msg_for_history: dict = {
            "role": "user",
            "content": f"[{ts_str}] {envelope.content}" + (" [image]" if image_atts else ""),
        }

        llm_history = _normalize_history(history)
        chat_messages = [
            {"role": "system", "content": system_prompt},
            *llm_history,
            user_msg_for_llm,
        ]

        chat_tools = self._get_chat_tool_definitions()

        _reply_tool = self.tool_registry.get_tool("reply_user")
        if _reply_tool is not None:
            if hasattr(_reply_tool, "_current_envelope"):
                _reply_tool._current_envelope = envelope
            if hasattr(_reply_tool, "_event_emitter"):
                _reply_tool._event_emitter = self._emit

        reply_text: str | None = None
        reply_image_id: str | None = None
        original_user_content = user_text
        preamble_size = len(chat_messages)
        prev_chat_tool_results: list[dict] = []
        all_evidence: list[dict] = []
        accumulated_summary = ""
        finished = False
        has_replied = False
        unanswered_result = False
        reply_send_count = 0
        reply_attempt_count = 0
        fabrication_retried = False
        overflow_retried = False
        prev_call_sig = ""
        dup_count = 0
        chat_budget = min(self.max_turns, settings.CHAT_MAX_TURNS)

        stopped = False
        turn = 0
        try:
            for turn in range(1, chat_budget + 1):
                _check_cancelled(self)
                self._set_state("chat_thinking", loop="chat")

                try:
                    text, tool_calls, sig = await _llm_call(
                        self, chat_messages, chat_tools,
                        loop="chat", chat_id=str(chat_id),
                        prev_tool_results=prev_chat_tool_results,
                    )
                except Exception as e:
                    logger.error("LLM error (chat): %s", e, exc_info=True)
                    if (
                        not overflow_retried
                        and classify_error(e, "chat_llm").category == ErrorCategory.CONTEXT_OVERFLOW
                    ):
                        overflow_retried = True
                        from .context_manager import ContextManager
                        before = len(chat_messages)
                        chat_messages, _ = ContextManager().emergency_truncate(
                            chat_messages, preamble_size,
                        )
                        shrunk = len(chat_messages) < before
                        if not shrunk and llm_history:
                            llm_history = _normalize_history(llm_history[len(llm_history) // 2:])
                            chat_messages = [
                                chat_messages[0], *llm_history, *chat_messages[preamble_size - 1:],
                            ]
                            preamble_size = 2 + len(llm_history)
                            shrunk = True
                        if shrunk:
                            _yield_missing_tool_results(chat_messages)
                            logger.info("Chat: context overflow — truncated, retrying once")
                            continue
                    _yield_missing_tool_results(chat_messages)
                    break

                if not tool_calls:
                    clean = _strip_meta_markers(text)
                    if _contains_raw_tool_call(clean):
                        logger.warning("Chat turn %d: stripping leaked tool-call markup from auto-reply", turn)
                        clean = _strip_raw_tool_calls(clean)
                    if (
                        (not has_replied or unanswered_result)
                        and clean
                        and clean not in ("(tools)", "(done)", "(no response)")
                    ):
                        logger.info("Chat turn %d: no tool calls, auto-replying with text", turn)
                        ar = await self._auto_reply(clean, chat_id, all_evidence, user_message=envelope.content, chat_history=history, channel=envelope.channel)
                        if ar["sent"]:
                            has_replied = True
                            unanswered_result = False
                            reply_text = clean
                            finished = True
                            break
                        if not fabrication_retried:
                            fabrication_retried = True
                            block_detail = ar.get("error", "") or "(no detail)"
                            logger.info(
                                "Chat turn %d: auto-reply blocked, injecting retry feedback: %s",
                                turn, block_detail[:160],
                            )
                            chat_messages.append(_build_assistant_msg(clean, [], sig))
                            chat_messages.append({
                                "role": "user",
                                "content": (
                                    f"[System] Your previous reply was BLOCKED by the fact verifier: "
                                    f"{block_detail}\n\n"
                                    f"You MUST call the appropriate tool to obtain real data BEFORE replying. "
                                    f"For health/activity questions (steps, distance, energy, sleep, heart rate, "
                                    f"stand time, etc.) call `sql` on the `health_data` database or `code`. "
                                    f"For write/action claims (create_page, update_md, schedule, memory writes) "
                                    f"call that tool first. Do NOT cite specific numbers, dates, or outcomes "
                                    f"from memory or prior turns — re-query for the current request. After the "
                                    f"tool returns, reply using only the values it produced."
                                ),
                            })
                            continue
                    finished = True
                    break

                reply_calls = [tc for tc in tool_calls if tc.get("name") == "reply_user"]
                if len(reply_calls) > 1:
                    merged_parts: list[str] = []
                    merged_args: dict[str, Any] = {}
                    for rtc in reply_calls:
                        args = _parse_arguments(rtc.get("arguments", {}))
                        msg = args.get("message", "")
                        if msg:
                            merged_parts.append(msg)
                        for key in ("image_path", "reply_to_message_id"):
                            if args.get(key) and not merged_args.get(key):
                                merged_args[key] = args[key]
                    merged_tc = {
                        "name": "reply_user",
                        "id": reply_calls[0].get("id", ""),
                        "arguments": {"message": "\n\n".join(merged_parts), **merged_args},
                    }
                    tool_calls = [tc for tc in tool_calls if tc.get("name") != "reply_user"] + [merged_tc]

                _ORDER = {"reply_user": 0, "finish_chat": 99, "manage": 40, "analyze": 50}
                tool_calls.sort(key=lambda tc: _ORDER.get(tc.get("name", ""), 10))

                chat_messages.append(_build_assistant_msg(_strip_meta_markers(text), tool_calls, sig))

                call_sig = _hash_tool_calls(tool_calls)
                if call_sig == prev_call_sig:
                    dup_count += 1
                    if dup_count >= 2:
                        logger.warning("Chat: 3 identical tool-call rounds, breaking loop")
                        _yield_missing_tool_results(chat_messages)
                        if not has_replied or unanswered_result:
                            clean = _strip_meta_markers(text)
                            if clean:
                                ar = await self._auto_reply(clean, chat_id, all_evidence, user_message=envelope.content, chat_history=history, channel=envelope.channel)
                                if ar["sent"]:
                                    has_replied = True
                                    unanswered_result = False
                                    reply_text = clean
                        break
                else:
                    dup_count = 0
                prev_call_sig = call_sig

                tool_results: list[dict] = []

                for tc in tool_calls:
                    tool_name = tc.get("name")
                    tc_id = tc.get("id", "")
                    arguments = _parse_arguments(tc.get("arguments", {}))

                    if tool_name not in CHAT_ALLOWED_TOOLS:
                        logger.warning("Chat: rejected tool %r (not available in this role)", tool_name)
                        tool_results.append({
                            "id": tc_id, "tool": tool_name, "arguments": arguments,
                            "result": dict(TOOL_NOT_AVAILABLE_RESULT),
                        })
                        continue

                    if tool_name == "analyze":
                        sub_goal = arguments.get("goal", "")
                        self._set_state("chat_analyzing", loop="chat")
                        await self._emit({"type": "chat_tool_call", "tool": "analyze",
                                          "arguments": arguments, "chat_id": chat_id,
                                          "call_id": tc_id, "status": "running",
                                          "summary": summarize_tool_args("analyze", arguments)})
                        try:
                            result = await asyncio.wait_for(
                                self._run_sub_analysis(
                                    sub_goal,
                                    chat_id=str(chat_id),
                                    source="chat",
                                ),
                                timeout=180.0,
                            )
                        except asyncio.TimeoutError:
                            result = {
                                "success": False, "findings": "",
                                "error": "Analysis timed out.", "evidence": [],
                            }
                        except Exception as exc:
                            result = {"success": False, "findings": "", "error": str(exc), "evidence": []}
                        if not result.get("success", True):
                            result = {
                                **result,
                                "error": (
                                    f"Analysis failed: {result.get('error') or 'unknown error'}. "
                                    "No health data was analysed — tell the user honestly "
                                    "and suggest trying again."
                                ),
                            }
                        all_evidence.extend(result.get("evidence", []))
                        unanswered_result = True
                        await self._emit({"type": "chat_tool_result", "tool": "analyze",
                                          "success": result.get("success", False),
                                          "result": {
                                              "findings": (result.get("findings") or "")[:500],
                                              **({"error": result["error"][:300]} if result.get("error") else {}),
                                          },
                                          "chat_id": chat_id, "call_id": tc_id,
                                          "status": "ok" if result.get("success", False) else "error",
                                          "result_preview": result_preview(
                                              result.get("findings") or result.get("error") or "")})
                        tool_results.append({"id": tc_id, "tool": "analyze", "result": result})
                        prev_chat_tool_results = tool_results
                        continue
                    elif tool_name == "manage":
                        sub_goal = arguments.get("goal", "")
                        self._set_state("chat_managing", loop="chat")
                        await self._emit({"type": "chat_tool_call", "tool": "manage",
                                          "arguments": arguments, "chat_id": chat_id,
                                          "call_id": tc_id, "status": "running",
                                          "summary": summarize_tool_args("manage", arguments)})
                        try:
                            result = await asyncio.wait_for(
                                self._run_chat_manage(sub_goal, str(chat_id)),
                                timeout=120.0,
                            )
                        except asyncio.TimeoutError:
                            result = {
                                "success": False, "error": "Management operation timed out.",
                                "result": "Management operation timed out.",
                                "actions_taken": [], "evidence": [],
                            }
                        except Exception as exc:
                            result = {
                                "success": False, "error": str(exc), "result": str(exc),
                                "actions_taken": [], "evidence": [],
                            }
                        all_evidence.extend(result.get("evidence", []))
                        unanswered_result = True
                        await self._emit({"type": "chat_tool_result", "tool": "manage",
                                          "success": result.get("success", False),
                                          "result": {"result": result.get("result", "")[:500]},
                                          "chat_id": chat_id, "call_id": tc_id,
                                          "status": "ok" if result.get("success", False) else "error",
                                          "result_preview": result_preview(result.get("result", ""))})
                        tool_results.append({"id": tc_id, "tool": "manage", "result": result})
                        prev_chat_tool_results = tool_results
                        continue
                    elif tool_name == "finish_chat":
                        if not has_replied:
                            tool_results.append({
                                "id": tc_id, "tool": "finish_chat",
                                "result": {
                                    "success": False,
                                    "error": "Cannot finish: no reply sent yet. Call reply_user first.",
                                },
                            })
                            continue
                        finished = True
                        tool_results.append({
                            "id": tc_id, "tool": "finish_chat",
                            "result": {"success": True, "message": "Chat session finished."},
                        })
                        continue
                    elif tool_name == "reply_user":
                        msg = arguments.get("message", "")
                        if not msg and text:
                            arguments["message"] = _strip_meta_markers(text)
                        elif msg:
                            arguments["message"] = _strip_meta_markers(msg)
                        if reply_send_count >= 2:
                            await _clear_reply_stream(self, str(chat_id))
                            tool_results.append({
                                "id": tc_id, "tool": "reply_user",
                                "result": {"success": True,
                                           "message": "Already sent 2 messages. Wrap up."},
                            })
                            continue
                        if reply_attempt_count >= 4 and not has_replied:
                            await _clear_reply_stream(self, str(chat_id))
                            tool_results.append({
                                "id": tc_id, "tool": "reply_user",
                                "result": {
                                    "success": False,
                                    "error": "Too many failed reply attempts — nothing was "
                                             "delivered. Stop retrying and finish.",
                                },
                            })
                            continue
                        reply_attempt_count += 1
                        arguments["chat_id"] = str(chat_id)

                    self._set_state(f"chat_executing:{tool_name}", loop="chat")
                    await self._emit({
                        "type": "chat_tool_call",
                        "tool": tool_name,
                        "arguments": arguments,
                        "sender": sender,
                        "chat_id": chat_id,
                        "call_id": tc_id, "status": "running",
                        "summary": summarize_tool_args(tool_name, arguments),
                    })

                    try:
                        evidence = _filter_data_evidence(all_evidence + tool_results) if tool_name == "reply_user" else None
                        result = await asyncio.wait_for(
                            self._execute_tool(
                                tool_name, arguments, evidence_trail=evidence,
                                user_message=envelope.content if tool_name in ("reply_user",) else "",
                                chat_history=history if tool_name == "reply_user" else None,
                            ),
                            timeout=60.0,
                        )
                    except asyncio.TimeoutError:
                        result = {"success": False, "error": "Tool timed out after 60s."}

                    await self._emit({
                        "type": "chat_tool_result",
                        "tool": tool_name,
                        "success": result.get("success", False),
                        "result": result,
                        "chat_id": chat_id,
                        "call_id": tc_id,
                        "status": "ok" if result.get("success", False) else "error",
                        "result_preview": result_preview(result),
                        **({"reply_text": arguments.get("message", "")[:200]} if tool_name == "reply_user" else {}),
                    })
                    tool_results.append({"id": tc_id, "tool": tool_name, "arguments": arguments, "result": result})
                    prev_chat_tool_results = tool_results

                    if tool_name == "reply_user" and not result.get("success"):
                        if result.get("blocked") == "verification":
                            await _clear_reply_stream(
                                self, str(chat_id), "verification",
                                str(result.get("verification_detail") or ""),
                            )
                        else:
                            await _clear_reply_stream(self, str(chat_id))
                    elif tool_name == "reply_user":
                        self._reply_streamed = False

                    if tool_name == "reply_user" and result.get("success"):
                        has_replied = True
                        unanswered_result = False
                        reply_send_count += 1
                        reply_text = arguments.get("message", "")
                        reply_image_id = result.get("image_id")

                    if finished:
                        break

                all_evidence.extend(tool_results)

                if finished:
                    break

                for tr in tool_results:
                    chat_messages.append(_build_tool_result_msg(tr["id"], tr["result"], tr["tool"]))

                try:
                    chat_messages, accumulated_summary = await _compress_context_if_needed(
                        chat_messages, preamble_size, self.context_window_size,
                        original_user_content, _ResilientProvider(self.llm), accumulated_summary,
                    )
                except Exception as exc:
                    logger.warning("Context compression error (chat): %s", exc)
        except asyncio.CancelledError:
            stopped = True

        if not stopped:
            await _clear_reply_stream(self, str(chat_id))

        if stopped:
            pass
        elif not has_replied:
            logger.warning("Chat with %s ended without reply, sending fallback", sender)
            await self._auto_reply(
                "Sorry, I couldn't process your message properly. Please try again!",
                chat_id, all_evidence, user_message=envelope.content,
                chat_history=history, channel=envelope.channel,
            )
            reply_text = "(auto-reply: processing failed)"
            finished = True
        elif unanswered_result and not finished:
            logger.warning("Chat with %s ended with an unreported result, sending fallback", sender)
            ar = await self._auto_reply(
                "Sorry, I couldn't finish putting the result together. Please try again!",
                chat_id, [], user_message=envelope.content,
                chat_history=history, channel=envelope.channel,
            )
            if ar.get("sent"):
                reply_text = "Sorry, I couldn't finish putting the result together. Please try again!"

        self._set_state("chat_complete", loop="chat")
        hist = self._chat_histories.setdefault(history_key, [])
        today_date = ts_str[:10]
        prev_user_date: str | None = None
        for m in reversed(hist):
            if m.get("role") == "user":
                mt = re.match(r"^\[(\d{4}-\d{2}-\d{2})", m.get("content", ""))
                if mt:
                    prev_user_date = mt.group(1)
                break
        hist.append(user_msg_for_history)
        if reply_text:
            if prev_user_date != today_date:
                stored_reply = f"({today_date}) {reply_text}"
            else:
                stored_reply = reply_text
            hist.append({"role": "assistant", "content": stored_reply})
        hist = _trim_history(hist, self._max_chat_history)
        self._chat_histories[history_key] = hist
        self._save_state()
        try:
            from .fact_verifier import _hash_message
            mem = self._chat_memory()
            turns: list[dict[str, Any]] = [{
                "role": "user",
                "content": envelope.content or ("[image]" if image_atts else ""),
                "client_msg_id": getattr(envelope, "message_id", None),
            }]
            if reply_text or reply_image_id:
                stored_content = reply_text or ""
                if reply_image_id:
                    img_md = f"![chart](/api/agent/chat-image/{reply_image_id})"
                    stored_content = f"{stored_content}\n\n{img_md}" if stored_content else img_md
                turns.append({
                    "role": "assistant",
                    "content": stored_content,
                    "message_hash": _hash_message(reply_text) if reply_text else None,
                    "image_id": reply_image_id,
                })
            spawn_background(
                mem.persist_chat_turns(history_key, turns),
                label="chat_history persist",
            )
        except Exception as e:
            logger.debug("chat_history persist skipped: %s", e)

        if stopped:
            if getattr(self, "_chat_stop_requested", False):
                await self._emit({"type": "chat_stopped", "chat_id": chat_id})
            self._set_state("chat_complete", loop="chat")
            logger.info("Chat with %s stopped by user (history=%d msgs)", sender, len(hist))
            raise asyncio.CancelledError()
        if reply_text and thread_id and thread_id != "main":
            from .chat_threads import auto_title_thread
            spawn_background(
                auto_title_thread(self, thread_id, envelope.content or "", reply_text),
                label="thread auto-title",
            )
        if reply_text and not getattr(self, "_plan_running", False):
            self._plan_running = True
            try:
                survey = await asyncio.to_thread(self._chat_memory().get_pending_survey)
                if survey:
                    spawn_background(
                        self._run_plan_designer_guarded(survey),
                        label="plan designer",
                    )
                else:
                    self._plan_running = False
            except Exception as e:
                self._plan_running = False
                logger.debug("plan-designer hook skipped: %s", e)
        logger.info("Chat with %s completed (%d turns, history=%d msgs)", sender, turn, len(hist))

    async def _auto_reply(
        self,
        text: str,
        chat_id: Any,
        tool_results: list[dict] | None = None,
        user_message: str = "",
        chat_history: list[dict] | None = None,
        channel: Any = None,
    ) -> dict:
        if _contains_raw_tool_call(text):
            text = _strip_raw_tool_calls(text)
        if not text:
            return {"sent": False, "error": ""}
        try:
            reply_tool = self.tool_registry.get_tool("reply_user")
            if reply_tool:
                if hasattr(reply_tool, "_current_tool_results"):
                    reply_tool._current_tool_results = list(
                        _filter_data_evidence(tool_results or [])
                    )
                if hasattr(reply_tool, "_current_user_message"):
                    reply_tool._current_user_message = user_message
                if hasattr(reply_tool, "_current_chat_history"):
                    reply_tool._current_chat_history = chat_history or []
                if hasattr(reply_tool, "_event_emitter"):
                    reply_tool._event_emitter = self._emit
                result = await reply_tool.execute(message=text, chat_id=str(chat_id))
                if result.get("success"):
                    if getattr(channel, "value", None) != "ios":
                        await self._emit({
                            "type": "chat_reply",
                            "content": text[:300],
                            "chat_id": str(chat_id),
                            "auto": True,
                        })
                    return {"sent": True, "error": ""}
                else:
                    err = str(result.get("error", ""))
                    logger.warning("Auto-reply failed: %s", err[:120])
                    return {"sent": False, "error": err}
        except Exception as e:
            logger.error("Auto-reply failed: %s", e)
            return {"sent": False, "error": str(e)}
        return {"sent": False, "error": ""}
