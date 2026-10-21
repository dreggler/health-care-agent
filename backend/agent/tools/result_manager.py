from __future__ import annotations

import json

MAX_RESULT_CHARS = 8000


def truncate_result(result: dict, tool_name: str) -> dict:
    result_str = json.dumps(result, ensure_ascii=False, default=str)
    if len(result_str) <= MAX_RESULT_CHARS:
        return result

    if tool_name == "code":
        return _truncate_code_result(result)
    elif tool_name == "sql":
        return _truncate_sql_result(result)
    else:
        return _generic_truncate(result, result_str)


def _truncate_code_result(result: dict) -> dict:
    result = dict(result)
    third = MAX_RESULT_CHARS // 3
    for field in ("output", "error"):
        text = result.get(field, "")
        if isinstance(text, str) and len(text) > MAX_RESULT_CHARS:
            head = text[:third]
            tail = text[-third:]
            omitted = len(text) - len(head) - len(tail)
            result[field] = f"{head}\n\n... [{omitted} characters omitted] ...\n\n{tail}"
            result["truncated"] = True
            result.setdefault("original_length", len(text))

    if len(json.dumps(result, ensure_ascii=False, default=str)) > MAX_RESULT_CHARS:
        return _generic_truncate(result, json.dumps(result, ensure_ascii=False, default=str))
    return result


def _truncate_sql_result(result: dict) -> dict:
    rows = result.get("rows", [])
    total = len(rows)
    if total <= 20:
        md = result.get("markdown", "")
        if len(md) > MAX_RESULT_CHARS:
            lines = md.split("\n")
            keep = lines[:22]
            result = dict(result)
            result["markdown"] = "\n".join(keep) + f"\n... ({total} rows total, truncated for context)"
            result["truncated"] = True
        return result

    result = dict(result)
    result["rows"] = rows[:20]
    result["truncated"] = True
    result["total_rows"] = total
    result["note"] = f"Showing 20 of {total} rows. Use LIMIT or WHERE in SQL for specific ranges."

    md = result.get("markdown", "")
    if md:
        lines = md.split("\n")
        keep = lines[:22]
        result["markdown"] = "\n".join(keep) + f"\n... ({total} rows total)"

    return result


def _generic_truncate(result: dict, result_str: str) -> dict:
    return {
        "success": result.get("success", True),
        "summary": result_str[:MAX_RESULT_CHARS],
        "truncated": True,
        "original_length": len(result_str),
        "note": "Result truncated due to size. Key data preserved above.",
    }
