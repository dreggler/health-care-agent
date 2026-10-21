from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class MessageChannel(str, Enum):

    TELEGRAM = "telegram"
    FEISHU = "feishu"
    WEIXIN = "weixin"
    IOS = "ios"
    WEB = "web"
    API = "api"
    BENCHMARK = "benchmark"


class CommandType(str, Enum):

    ASK = "ask"
    HELP = "help"
    CLEAR = "clear"
    MUTE = "mute"
    UNMUTE = "unmute"
    RESTART = "restart"
    LOG = "log"
    UNKNOWN = "unknown"


@dataclass
class MessageEnvelope:

    message_id: str
    channel: MessageChannel
    sender_id: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    reply_to: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    chat_id: str | None = None
    telegram_message_id: int | None = None

    conversation_id: str | None = None
    platform_message_id: str | None = None

    attachments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ParsedCommand:

    command_type: CommandType
    args: str = ""
    raw_text: str = ""
    envelope: MessageEnvelope | None = None


class BaseGateway(abc.ABC):

    channel: MessageChannel

    default_chat_id: str | None = None

    allowed_chat_ids: set[str] | None = None

    supports_inline_caption: bool = True


    @abc.abstractmethod
    async def start(self) -> None:
        pass

    @abc.abstractmethod
    async def stop(self) -> None:
        pass


    @abc.abstractmethod
    async def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        pass

    @abc.abstractmethod
    async def send_photo(
        self,
        photo_path: str,
        caption: str = "",
        chat_id: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        pass

    @abc.abstractmethod
    async def edit_message(
        self,
        chat_id: str,
        message_id: Any,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        pass

    @abc.abstractmethod
    async def answer_callback(
        self,
        callback_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> bool:
        pass


    def is_muted(self) -> bool:
        return False
