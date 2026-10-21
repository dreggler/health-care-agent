from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_EVIDENCE_ITEMS = 10
_MAX_EVIDENCE_CHARS = 8000

_WRITE_KEYWORDS = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", re.IGNORECASE
)


def _hash_message(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _classify_scenario(
    tool_results: list[dict[str, Any]],
) -> str:
    for tr in tool_results:
        tool = tr.get("tool", "")
        if tool == "code":
            return "health_analysis"
        if tool == "sql":
            query = tr.get("arguments", {}).get("query", "")
            if query.startswith("memory:"):
                continue
            raw_query = query.split(":", 1)[-1] if ":" in query else query
            if not _WRITE_KEYWORDS.match(raw_query):
                return "health_analysis"
    return "operation"


def _format_tool_evidence(tool_results: list[dict[str, Any]]) -> list[str]:
    if not tool_results:
        return ["_No tool call evidence recorded._"]

    _MAX_TABLE_ROWS = 8

    sections: list[str] = []
    for i, tr in enumerate(tool_results[:_MAX_EVIDENCE_ITEMS]):
        tool_name = tr.get("tool", "unknown")
        result = tr.get("result", {})
        success = result.get("success", False)

        label = {
            "sql": "Data Query",
            "code": "Analysis",
        }.get(tool_name, tool_name.replace("_", " ").title())

        header = f"\U0001f4cb *Step {i + 1}* \u2014 {label}"

        if not success:
            error = result.get("error", "unknown error")
            sections.append(f"{header}\n\u274c Error: {error[:150]}")
            continue

        if tool_name == "sql" and "markdown" in result:
            row_count = result.get("row_count", 0)
            md = result["markdown"].strip()
            md_lines = md.split("\n")
            if len(md_lines) > _MAX_TABLE_ROWS + 2:
                md_lines = md_lines[: _MAX_TABLE_ROWS + 2]
                md_lines.append(f"  ... ({row_count} rows total)")
            table_text = "\n".join(md_lines)
            sections.append(f"{header} ({row_count} rows)\n```\n{table_text}\n```")

        elif tool_name == "code" and result.get("output"):
            output = result["output"].strip()[:400]
            sections.append(f"{header}\n```\n{output}\n```")

        elif tool_name == "sql":
            row_count = result.get("row_count", 0)
            sections.append(f"{header} ({row_count} rows affected)")

        else:
            brief = json.dumps(result, default=str)[:250]
            sections.append(f"{header}\n```\n{brief}\n```")

    if len(tool_results) > _MAX_EVIDENCE_ITEMS:
        sections.append(
            f"_\u2026 and {len(tool_results) - _MAX_EVIDENCE_ITEMS} more steps_"
        )
    return sections


class FactVerifier:

    def __init__(self, memory_db_path: Path, user_id: str) -> None:
        self.memory_db_file = memory_db_path / f"{user_id}.db"
        self.user_id = user_id
        self._ensure_unique_constraint()

    def _ensure_unique_constraint(self) -> None:
        try:
            with sqlite3.connect(str(self.memory_db_file), timeout=10) as conn:
                try:
                    conn.execute(
                        "DELETE FROM message_evidence WHERE id NOT IN ("
                        "  SELECT MAX(id) FROM message_evidence GROUP BY message_hash"
                        ")"
                    )
                except sqlite3.Error as exc:
                    logger.debug("message_evidence de-duplication skipped: %s", exc)
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_message_evidence_hash_unique "
                    "ON message_evidence(message_hash)"
                )
                conn.commit()
        except Exception as exc:
            logger.debug("Could not ensure unique constraint on message_evidence: %s", exc)

    def store_evidence(
        self,
        message_text: str,
        tool_results: list[dict[str, Any]],
        verification_status: str = "verified",
        verification_detail: str = "",
    ) -> str:
        msg_hash = _hash_message(message_text)
        items = tool_results[-_MAX_EVIDENCE_ITEMS:]
        while items and len(json.dumps(items, default=str)) > _MAX_EVIDENCE_CHARS:
            items = items[1:]
        evidence_json = json.dumps(items, default=str) if items else "[]"

        try:
            with sqlite3.connect(str(self.memory_db_file), timeout=10) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO message_evidence "
                    "(message_hash, message_text, tool_calls, verification_status, verification_detail) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        msg_hash,
                        message_text[:4200],
                        evidence_json,
                        verification_status,
                        verification_detail[:2000],
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.debug("Failed to store evidence: %s", exc)

        return msg_hash

    def get_evidence(self, message_hash: str) -> dict[str, Any] | None:
        try:
            with sqlite3.connect(str(self.memory_db_file), timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM message_evidence WHERE message_hash = ? ORDER BY id DESC LIMIT 1",
                    (message_hash,),
                ).fetchone()
                if row:
                    d = dict(row)
                    try:
                        d["tool_calls"] = json.loads(d["tool_calls"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                    return d
        except Exception as exc:
            logger.debug("Failed to retrieve evidence: %s", exc)
        return None

    def format_evidence_for_display(self, evidence: dict[str, Any]) -> str:
        tool_calls = evidence.get("tool_calls", [])
        status = evidence.get("verification_status", "unknown")

        status_line = {
            "verified": "\u2705 Verified",
            "unverified": "\u26a0\ufe0f Unverified",
            "fabricated": "\u274c Fabricated \u2014 data claims without evidence",
            "no_evidence_needed": "\u2705 No data claims",
            "failed": "\u274c Failed",
        }.get(status, f"\u2753 {status}")

        detail = evidence.get("verification_detail", "")
        header = f"\U0001f4ca *Evidence Trail*\n{status_line}"
        if detail:
            header += f"\n_{detail}_"
        parts = [header]

        if isinstance(tool_calls, list) and tool_calls:
            parts.extend(_format_tool_evidence(tool_calls))
        else:
            parts.append("_No tool call evidence recorded._")

        text = "\n\n".join(parts)

        if len(text) > 3900:
            text = text[:3900] + "\n\n_\u2026evidence truncated_"
        return text


    async def verify_message(
        self,
        message_text: str,
        tool_results: list[dict[str, Any]],
        llm_provider=None,
        user_message: str = "",
        chat_history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        has_evidence = bool(tool_results)

        if not has_evidence:
            verification_status = "no_evidence_needed"
            verification_detail = "No tool calls (LLM check unavailable)."

            if llm_provider:
                try:
                    fab_result = await asyncio.wait_for(
                        self._llm_detect_fabrication(
                            message_text, llm_provider, user_message=user_message,
                            chat_history=chat_history,
                        ),
                        timeout=10.0,
                    )
                    if fab_result.get("has_claims"):
                        verification_status = "fabricated"
                        verification_detail = fab_result.get(
                            "detail",
                            "Message contains data claims without supporting tool calls.",
                        )
                    else:
                        verification_status = "no_evidence_needed"
                        verification_detail = "Conversational message, no data claims."
                except (asyncio.TimeoutError, Exception) as exc:
                    logger.debug("Fabrication detection skipped: %s", exc)
                    verification_status = "no_evidence_needed"
                    verification_detail = "Fabrication check skipped, defaulting to allow."

            msg_hash = await asyncio.to_thread(
                self.store_evidence,
                message_text, tool_results,
                verification_status=verification_status,
                verification_detail=verification_detail,
            )
            return {
                "verified": verification_status == "no_evidence_needed",
                "status": verification_status,
                "detail": verification_detail,
                "message_hash": msg_hash,
            }

        scenario = _classify_scenario(tool_results)

        verification_detail = f"Based on {len(tool_results)} tool call(s)."
        verification_status = "verified"
        selected_evidence = tool_results

        if llm_provider:
            try:
                if scenario == "health_analysis":
                    verify_fn = self._llm_verify_health
                else:
                    verify_fn = self._llm_verify_operation

                verification_result = await asyncio.wait_for(
                    verify_fn(message_text, tool_results, llm_provider),
                    timeout=10.0,
                )
                verification_status = verification_result.get("status", "verified")
                verification_detail = verification_result.get("detail", verification_detail)
                indices = verification_result.get("evidence_indices")
                if indices:
                    picked = [
                        tool_results[i] for i in indices
                        if 0 <= i < len(tool_results)
                    ]
                    if picked:
                        selected_evidence = picked
            except (asyncio.TimeoutError, Exception) as exc:
                logger.debug("LLM verification skipped: %s", exc)
                verification_detail += " (LLM verification skipped)"

        msg_hash = await asyncio.to_thread(
            self.store_evidence,
            message_text, selected_evidence,
            verification_status=verification_status,
            verification_detail=verification_detail,
        )
        return {
            "verified": verification_status == "verified",
            "status": verification_status,
            "detail": verification_detail,
            "message_hash": msg_hash,
        }


    async def _llm_detect_fabrication(
        self,
        message_text: str,
        llm_provider,
        user_message: str = "",
        chat_history: list[dict[str, Any]] | None = None,
    ) -> dict:
        from .prompt_loader import load_and_format

        user_ctx = ""
        if user_message:
            user_ctx = f"USER MESSAGE (context):\n{user_message[:300]}\n\n"

        history_ctx = ""
        if chat_history:
            history_ctx = self._format_chat_history_for_verification(chat_history)

        messages = [{
            "role": "user",
            "content": load_and_format(
                "verify_chat.md",
                user_ctx=user_ctx,
                history_ctx=history_ctx,
                agent_reply=message_text[:500],
            ),
        }]

        response_parts: list[str] = []
        async for chunk in llm_provider.complete(
            messages=messages,
            tools=None,
            stream=True,
            max_tokens=1024,
            temperature=0.0,
        ):
            if chunk.get("type") == "content":
                response_parts.append(chunk["content"])

        text = "".join(response_parts).strip()

        r = None
        match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if match:
            try:
                r = json.loads(match.group())
            except (json.JSONDecodeError, ValueError):
                pass
        if r is None:
            c_match = re.search(r'"c"\s*:\s*"([yn])"', text)
            if c_match:
                r = {"c": c_match.group(1)}
                d_match = re.search(r'"d"\s*:\s*"((?:[^"\\]|\\.)*)', text)
                if d_match:
                    r["d"] = d_match.group(1)[:200]

        if r is not None:
            has_claims = r.get("c") == "y"
            detail = r.get("d", "Contains unsupported data claims.")[:200] if has_claims else ""
            return {"has_claims": has_claims, "detail": detail}

        return {"has_claims": False, "detail": ""}

    @staticmethod
    def _format_chat_history_for_verification(
        chat_history: list[dict[str, Any]],
    ) -> str:
        if not chat_history:
            return ""
        assistant_msgs: list[str] = []
        for msg in chat_history[-10:]:
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if content:
                    assistant_msgs.append(content[:300])
        if not assistant_msgs:
            return ""
        history_text = "\n---\n".join(assistant_msgs)
        return (
            f"CONVERSATION HISTORY (previous verified assistant messages):\n"
            f"{history_text}\n\n"
        )


    async def _llm_verify_health(
        self,
        message_text: str,
        tool_results: list[dict[str, Any]],
        llm_provider,
    ) -> dict:
        recent = tool_results[-_MAX_EVIDENCE_ITEMS:]
        offset = max(0, len(tool_results) - _MAX_EVIDENCE_ITEMS)

        summaries: list[str] = []
        valid_indices: list[int] = []
        for i, tr in enumerate(recent):
            original_i = i + offset
            result = tr.get("result", {})
            if not result.get("success"):
                continue
            tool = tr.get("tool", "")
            args = tr.get("arguments", {})
            if tool == "sql" and "markdown" in result:
                query = args.get("query", "")
                db = args.get("database", "health_data")
                row_count = result.get("row_count", 0)
                md_lines = result["markdown"].strip().split("\n")[:12]
                header = f"[{i}] SQL query on {db} ({row_count} rows):\n```\n{query[:500]}\n```\nResult:\n"
                summaries.append(header + "\n".join(md_lines))
                valid_indices.append(original_i)
            elif tool == "code" and result.get("output"):
                code_snippet = args.get("code", "")[:500]
                output = result["output"][:3000]
                summaries.append(f"[{i}] Code:\n```\n{code_snippet}\n```\nOutput:\n{output}")
                valid_indices.append(original_i)

        if not summaries:
            return {
                "status": "verified",
                "detail": "No numerical data to cross-check.",
                "evidence_indices": valid_indices,
            }

        data_str = "\n".join(summaries)[:80000]

        from .prompt_loader import load_and_format

        messages = [{
            "role": "user",
            "content": load_and_format(
                "verify_health.md",
                message_text=message_text[:2000],
                data_str=data_str,
            ),
        }]

        return await self._call_llm_and_parse(
            llm_provider, messages, valid_indices, offset,
        )


    async def _llm_verify_operation(
        self,
        message_text: str,
        tool_results: list[dict[str, Any]],
        llm_provider,
    ) -> dict:
        recent = tool_results[-_MAX_EVIDENCE_ITEMS:]
        offset = max(0, len(tool_results) - _MAX_EVIDENCE_ITEMS)

        summaries: list[str] = []
        valid_indices: list[int] = []
        for i, tr in enumerate(recent):
            original_i = i + offset
            tool = tr.get("tool", "")
            result = tr.get("result", {})
            success = result.get("success", False)
            status_str = "OK" if success else f"ERROR: {result.get('error', '?')[:80]}"

            if tool == "sql":
                query = tr.get("arguments", {}).get("query", "")
                row_count = result.get("row_count", 0)
                summaries.append(
                    f"[{i}] SQL: {query[:500]}\n"
                    f"    → {status_str}, {row_count} rows affected"
                )
            else:
                brief = json.dumps(result, default=str)[:1000]
                summaries.append(f"[{i}] {tool} → {status_str}\n    {brief}")
            valid_indices.append(original_i)

        if not summaries:
            return {
                "status": "verified",
                "detail": "No actions to cross-check.",
                "evidence_indices": valid_indices,
            }

        data_str = "\n".join(summaries)[:80000]

        from .prompt_loader import load_and_format

        messages = [{
            "role": "user",
            "content": load_and_format(
                "verify_operation.md",
                message_text=message_text[:2000],
                data_str=data_str,
            ),
        }]

        return await self._call_llm_and_parse(
            llm_provider, messages, valid_indices, offset,
        )


    async def _call_llm_and_parse(
        self,
        llm_provider,
        messages: list[dict],
        valid_indices: list[int],
        offset: int,
    ) -> dict:
        response_parts: list[str] = []
        async for chunk in llm_provider.complete(
            messages=messages,
            tools=None,
            stream=True,
            max_tokens=1024,
            temperature=0.0,
        ):
            if chunk.get("type") == "content":
                response_parts.append(chunk["content"])

        text = "".join(response_parts).strip()

        parsed = self._parse_verification_json(text)
        if parsed is not None:
            status = "unverified" if parsed.get("s") == "n" else "verified"
            detail = str(parsed.get("d", ""))[:200]
            raw_e = parsed.get("e")
            if raw_e is not None:
                evidence = [int(x) + offset for x in raw_e if isinstance(x, (int, float))]
            else:
                evidence = list(valid_indices)
            return {
                "status": status,
                "detail": detail,
                "evidence_indices": evidence or valid_indices,
            }

        return {
            "status": "verified",
            "detail": "Check inconclusive.",
            "evidence_indices": valid_indices,
        }

    @staticmethod
    def _parse_verification_json(text: str) -> dict | None:
        match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except (json.JSONDecodeError, ValueError):
                pass

        s_match = re.search(r'"s"\s*:\s*"([yn])"', text)
        if s_match:
            result: dict = {"s": s_match.group(1)}
            d_match = re.search(r'"d"\s*:\s*"((?:[^"\\]|\\.)*)', text)
            if d_match:
                result["d"] = d_match.group(1)[:200]
            e_match = re.search(r'"e"\s*:\s*\[([0-9,\s]*)\]', text)
            if e_match:
                try:
                    result["e"] = [int(x.strip()) for x in e_match.group(1).split(",") if x.strip()]
                except ValueError:
                    pass
            return result

        return None
