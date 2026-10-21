import json
import logging
import re

from .tools.base import load_tool_definitions
from .tools.result_manager import truncate_result

logger = logging.getLogger(__name__)


def _salvage_truncated_json(raw: str) -> dict:
    result: dict = {}
    for m in re.finditer(
        r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)"\s*[,}]',
        raw,
    ):
        result[m.group(1)] = m.group(2).replace('\\"', '"').replace("\\n", "\n")

    last_match = None
    for m in re.finditer(r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*?)$', raw):
        last_match = m
    if last_match:
        key = last_match.group(1)
        if key not in result:
            val = last_match.group(2).replace('\\"', '"').replace("\\n", "\n")
            if val:
                result[key] = val

    if result:
        logger.info(
            "Salvaged %d fields from truncated JSON: %s",
            len(result), list(result.keys()),
        )
    return result


class AgentToolsMixin:


    def _group_tool_calls_by_concurrency(self, tool_calls: list) -> list:
        groups: list = []
        current_safe: list = []

        for tc in tool_calls:
            tool_name = tc.get("name", "")
            tool = self.tool_registry.get_tool(tool_name)
            is_safe = getattr(tool, "is_concurrency_safe", False) if tool else False

            if is_safe:
                current_safe.append(tc)
            else:
                if current_safe:
                    groups.append((True, current_safe))
                    current_safe = []
                groups.append((False, [tc]))

        if current_safe:
            groups.append((True, current_safe))

        return groups

    def _get_tool_definitions(self) -> list:
        return self.tool_registry.get_definitions()

    def _get_chat_tool_definitions(self) -> list:
        excluded = {
            "push_report", "sql", "code", "read_skill",
            "update_md", "create_page",
        }
        defs = []
        for t in self.tool_registry.get_all_tools():
            defn = t.get_definition()
            name = defn.get("function", {}).get("name")
            if not name:
                logger.error("Tool %s has invalid definition, skipping", type(t).__name__)
                continue
            if name not in excluded:
                defs.append(defn)
        manage_def = self._load_tool_json("manage")
        if manage_def:
            defs.insert(0, manage_def)
        analyze_def = self._load_tool_json("analyze")
        if analyze_def:
            defs.insert(0, analyze_def)
        return defs

    def _get_sub_analysis_tool_definitions(
        self, extra_tools: set[str] | None = None,
    ) -> list:
        allowed = {"sql", "code", "read_skill"}
        if extra_tools:
            allowed |= extra_tools
        defs = []
        for t in self.tool_registry.get_all_tools():
            defn = t.get_definition()
            name = defn.get("function", {}).get("name")
            if not name:
                logger.error("Tool %s has invalid definition, skipping", type(t).__name__)
                continue
            if name in allowed:
                defs.append(defn)
        return defs

    def _get_sub_manage_tool_definitions(self) -> list:
        allowed = {"sql", "update_md", "create_page"}
        defs = []
        for t in self.tool_registry.get_all_tools():
            defn = t.get_definition()
            name = defn.get("function", {}).get("name")
            if not name:
                logger.error("Tool %s has invalid definition, skipping", type(t).__name__)
                continue
            if name in allowed:
                defs.append(defn)
        return defs

    @staticmethod
    def _load_tool_json(tool_name: str) -> dict:
        import copy
        return copy.deepcopy(load_tool_definitions().get(tool_name, {}))

    def _feed_evidence_to_tool(
        self, tool_name: str, tool_results: list, user_message: str = "",
        chat_history: list | None = None,
    ) -> None:
        if tool_name in ("push_report", "reply_user"):
            tool = self.tool_registry.get_tool(tool_name)
            if tool and hasattr(tool, "_current_tool_results"):
                tool._current_tool_results = list(tool_results)
            if tool and hasattr(tool, "_current_user_message"):
                tool._current_user_message = user_message
            if tool and hasattr(tool, "_current_chat_history"):
                tool._current_chat_history = list(chat_history or [])
            if tool and hasattr(tool, "_event_emitter"):
                tool._event_emitter = self._emit

    async def _execute_tool(
        self, tool_name: str, arguments: dict,
        evidence_trail: list = None, user_message: str = "",
        chat_history: list | None = None,
    ) -> dict:
        tool = self.tool_registry.get_tool(tool_name)
        if not tool:
            hint = ""
            if ":" in tool_name and "sql" in tool_name:
                hint = (
                    " Hint: the tool name is just 'sql'. Put the database prefix "
                    "inside the query parameter, e.g. sql(query='memory:SELECT ...')"
                )
            return {"success": False, "error": f"Unknown tool: {tool_name}.{hint}"}

        raw_fallback = arguments.pop("_raw", None)
        if raw_fallback and not arguments:
            arguments = _salvage_truncated_json(raw_fallback)

        if evidence_trail is not None:
            self._feed_evidence_to_tool(tool_name, evidence_trail, user_message=user_message, chat_history=chat_history)

        try:
            if tool_name == "push_report":
                current_sim_time = getattr(self, "current_simulation_timestamp", None)
                if current_sim_time:
                    meta = arguments.get("metadata")
                    if meta is None:
                        meta = {}
                    elif isinstance(meta, str):
                        try:
                            meta = json.loads(meta)
                        except json.JSONDecodeError:
                            meta = {}
                    if isinstance(meta, dict):
                        meta["data_timestamp"] = current_sim_time
                        arguments["metadata"] = meta

            if tool.input_schema is not None:
                return await tool(**arguments)
            return await tool.execute(**arguments)

        except Exception as e:
            logger.error("Tool execution error (%s): %s", tool_name, e, exc_info=True)
            return {"success": False, "error": str(e), "error_type": type(e).__name__}

    @staticmethod
    def _format_tool_result(tool_name: str, result: dict, arguments: dict = None) -> str:
        result = truncate_result(result, tool_name)
        query_label = ""
        if arguments:
            if tool_name == "sql" and "query" in arguments:
                q = arguments["query"]
                if ":" in q:
                    _, sql_part = q.split(":", 1)
                    query_label = f" — `{sql_part.strip()[:120]}`"
                else:
                    query_label = f" — `{q[:120]}`"
            elif tool_name == "code" and "code" in arguments:
                first_line = arguments["code"].strip().split("\n")[0][:80]
                query_label = f" — `{first_line}`"

        if tool_name == "sql" and result.get("success") and "markdown" in result:
            md = result["markdown"]
            meta_parts = [f"{result.get('row_count', 0)} rows"]
            if result.get("truncated"):
                meta_parts.append("truncated")
            meta = ", ".join(meta_parts)
            return f"Tool `sql`{query_label} returned ({meta}):\n\n{md}"
        return f"Tool `{tool_name}`{query_label} returned:\n```json\n{json.dumps(result, indent=2, default=str)}\n```"
