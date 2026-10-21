from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from ..messaging.base import BaseGateway, MessageChannel
from .connections import ios_connections
from .image_store import image_store
from .threads import thread_id_from_chat_id

logger = logging.getLogger(__name__)

_APP_NAME = "CareAgent"
_APNS_COALESCE_S = 5.0


def extract_message_hash(reply_markup: dict[str, Any] | None) -> str | None:
    if not reply_markup:
        return None
    try:
        for row in reply_markup.get("inline_keyboard", []) or []:
            for btn in row:
                data = btn.get("callback_data", "") or ""
                if data.startswith("evidence:"):
                    return data.split(":", 1)[1]
    except Exception:
        pass
    return None


class IOSGateway(BaseGateway):

    channel = MessageChannel.IOS
    supports_inline_caption = True

    def __init__(self, user_id: str, apns_sender: Any | None = None) -> None:
        self.user_id = user_id
        self.default_chat_id = user_id
        self.allowed_chat_ids = {user_id}
        self.sender = None
        self._apns = apns_sender
        self._last_apns_ts: float | None = None
        self.last_image_id: str | None = None


    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


    async def _emit_to_stream(self, event: dict) -> bool:
        from ..api.agent_state import active_agents

        info = active_agents.get(self.user_id)
        agent = info.get("agent") if info else None
        if agent is None:
            logger.warning(
                "IOSGateway[user=%s]: no active agent to emit %s",
                self.user_id, event.get("type"),
            )
            return False
        try:
            await agent._emit(event)
            return True
        except Exception as e:
            logger.warning("IOSGateway[user=%s]: emit failed: %s", self.user_id, e)
            return False

    async def _maybe_push_apns(
        self, body: str, data: dict, time_sensitive: bool = False,
    ) -> None:
        if ios_connections.is_online(self.user_id):
            return
        if self._apns is None or not getattr(self._apns, "enabled", False):
            logger.info(
                "IOSGateway[user=%s]: offline and APNs not configured — "
                "message persisted, will appear on next app open",
                self.user_id,
            )
            return
        now = time.monotonic()
        last = self._last_apns_ts
        if last is not None and now - last < _APNS_COALESCE_S:
            return
        self._last_apns_ts = now
        try:
            extra = {"time_sensitive": True} if time_sensitive else {}
            await self._apns.send(
                self.user_id, title=_APP_NAME, body=body, data=data, **extra,
            )
        except Exception as e:
            logger.warning("IOSGateway[user=%s]: APNs send failed: %s", self.user_id, e)
            if self._last_apns_ts == now:
                self._last_apns_ts = last

    @staticmethod
    def _push_data(target: str, thread_id: str, msg_hash: str | None) -> dict:
        data: dict = {"chat_id": target, "message_hash": msg_hash}
        if thread_id != "main":
            data["thread_id"] = thread_id
        return data


    async def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        report_id: int | None = None,
    ) -> bool:
        target = chat_id or self.default_chat_id
        thread_id = thread_id_from_chat_id(target, self.user_id)
        msg_hash = extract_message_hash(reply_markup)
        delivered = await self._emit_to_stream({
            "type": "chat_reply",
            "content": text,
            "chat_id": target,
            "thread_id": thread_id,
            "message_hash": msg_hash,
            "reply_markup": reply_markup,
            "report_id": report_id,
            "auto": False,
            "final": True,
        })
        await self._maybe_push_apns(
            body=(text or "")[:120],
            data=self._push_data(target, thread_id, msg_hash),
            time_sensitive=report_id is not None,
        )
        return delivered

    async def send_photo(
        self,
        photo_path: str,
        caption: str = "",
        chat_id: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        target = chat_id or self.default_chat_id
        thread_id = thread_id_from_chat_id(target, self.user_id)
        msg_hash = extract_message_hash(reply_markup)
        image_id = None
        self.last_image_id = None
        try:
            if photo_path and os.path.exists(photo_path):
                image_id = await asyncio.to_thread(
                    image_store.register, self.user_id, photo_path,
                )
                self.last_image_id = image_id
        except Exception as e:
            logger.warning("IOSGateway[user=%s]: image register failed: %s", self.user_id, e)
        delivered = await self._emit_to_stream({
            "type": "chat_image",
            "image_id": image_id,
            "url": f"/api/agent/chat-image/{image_id}" if image_id else None,
            "caption": caption,
            "chat_id": target,
            "thread_id": thread_id,
            "message_hash": msg_hash,
            "reply_markup": reply_markup,
        })
        await self._maybe_push_apns(
            body=(caption or "📷 Image")[:120],
            data=self._push_data(target, thread_id, msg_hash),
        )
        return delivered

    async def edit_message(
        self,
        chat_id: str,
        message_id: Any,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        return True

    async def answer_callback(
        self,
        callback_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> bool:
        return True

    def is_muted(self) -> bool:
        return False
