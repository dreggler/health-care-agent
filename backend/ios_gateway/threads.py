from __future__ import annotations

import re

MAIN_THREAD_ID = "main"
MAIN_THREAD_TITLE = "CareAgent"
_THREAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def is_valid_thread_id(thread_id: str | None) -> bool:
    return isinstance(thread_id, str) and (
        thread_id == MAIN_THREAD_ID or bool(_THREAD_ID_RE.match(thread_id))
    )


def chat_id_for(user_id: str, thread_id: str | None) -> str:
    if not thread_id or thread_id == MAIN_THREAD_ID:
        return user_id
    return f"{user_id}:{thread_id}"


def history_key_for(user_id: str, thread_id: str | None) -> str:
    return f"ios:{chat_id_for(user_id, thread_id)}"


def thread_id_from_chat_id(chat_id: object, user_id: str) -> str:
    prefix = f"{user_id}:"
    if isinstance(chat_id, str) and chat_id.startswith(prefix):
        tid = chat_id[len(prefix):]
        if _THREAD_ID_RE.match(tid):
            return tid
    return MAIN_THREAD_ID


def thread_id_from_history_key(history_key: str) -> str | None:
    if not history_key.startswith("ios:"):
        return None
    parts = history_key.split(":")
    if len(parts) >= 3 and _THREAD_ID_RE.match(parts[-1]):
        return parts[-1]
    return MAIN_THREAD_ID
