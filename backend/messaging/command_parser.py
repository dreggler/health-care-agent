from __future__ import annotations

import logging

from .base import CommandType, MessageEnvelope, ParsedCommand

logger = logging.getLogger(__name__)

_COMMAND_MAP = {
    "/help": CommandType.HELP,
    "/clear": CommandType.CLEAR,
    "/mute": CommandType.MUTE,
    "/unmute": CommandType.UNMUTE,
    "/restart": CommandType.RESTART,
    "/log": CommandType.LOG,
}


class CommandParser:

    @staticmethod
    def parse(text: str, envelope: MessageEnvelope | None = None) -> ParsedCommand:
        stripped = text.strip()
        if not stripped:
            return ParsedCommand(
                command_type=CommandType.UNKNOWN,
                raw_text=text,
                envelope=envelope,
            )

        if stripped.startswith("/"):
            parts = stripped.split(None, 1)
            token = parts[0].split("@")[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            cmd_type = _COMMAND_MAP.get(token, CommandType.UNKNOWN)
            return ParsedCommand(
                command_type=cmd_type,
                args=args,
                raw_text=text,
                envelope=envelope,
            )

        return ParsedCommand(
            command_type=CommandType.ASK,
            args=stripped,
            raw_text=text,
            envelope=envelope,
        )
