from __future__ import annotations

from typing import Any

_FEISHU_ELEMENT_CHAR_LIMIT = 1800


def _plain_text_element(text: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": text or "",
        },
    }


def _split_text_elements(text: str) -> list[dict[str, Any]]:
    if not text or len(text) <= _FEISHU_ELEMENT_CHAR_LIMIT:
        return [_plain_text_element(text)]

    elements: list[dict[str, Any]] = []
    chunks = _split_to_chunks(text, _FEISHU_ELEMENT_CHAR_LIMIT)
    for chunk in chunks:
        elements.append(_plain_text_element(chunk))
    return elements


def _split_to_chunks(text: str, limit: int) -> list[str]:
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(para) > limit:
                sub_lines = para.split("\n")
                current = ""
                for line in sub_lines:
                    candidate2 = f"{current}\n{line}" if current else line
                    if len(candidate2) <= limit:
                        current = candidate2
                    else:
                        if current:
                            chunks.append(current)
                        while len(line) > limit:
                            chunks.append(line[:limit])
                            line = line[limit:]
                        current = line
            else:
                current = para

    if current:
        chunks.append(current)
    return chunks


def _button(text: str, action: str, message_hash: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {
            "tag": "plain_text",
            "content": text,
        },
        "type": "primary",
        "value": {
            "action": action,
            "hash": message_hash,
        },
    }


def _header(title: str, emoji: str = "") -> dict[str, Any]:
    title_text = f"{emoji} {title}".strip() if emoji else title
    return {
        "template": "blue",
        "title": {
            "tag": "plain_text",
            "content": title_text,
        },
    }


def build_plain_card(message_text: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "elements": _split_text_elements(message_text),
    }


def build_evidence_card(
    message_text: str,
    message_hash: str,
    status_emoji: str = "\U0001f4ca",
) -> dict[str, Any]:
    body_elements = _split_text_elements(message_text)
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("Hime", status_emoji),
        "elements": body_elements + [
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    _button("Show Evidence", "evidence", message_hash),
                ],
            },
        ],
    }


def build_back_card(message_text: str, message_hash: str) -> dict[str, Any]:
    body_elements = _split_text_elements(message_text)
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("Evidence", "\U0001f50d"),
        "elements": body_elements + [
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    _button("\u21a9 Back", "restore", message_hash),
                ],
            },
        ],
    }
