from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..messaging.base import BaseGateway, MessageChannel, MessageEnvelope
from .qr_login import load_bot_record, resolve_base_url
from .sender import WeixinSender
from .transport import WeixinPoller

logger = logging.getLogger(__name__)

OnUserMessage = Callable[[MessageEnvelope], Awaitable[None]]


class WeixinGateway(BaseGateway):

    channel = MessageChannel.WEIXIN
    supports_inline_caption = False

    def __init__(
        self,
        settings: Any,
        on_user_message: OnUserMessage | None = None,
        allowed_user_ids: set[str] | None = None,
    ) -> None:
        self._settings = settings
        self._on_user_message = on_user_message

        token_path = Path(
            getattr(
                settings,
                "WEIXIN_BOT_TOKEN_PATH",
                "./data/weixin_bot_token.json",
            )
        )
        self._token_path = token_path
        record = load_bot_record(token_path)
        self._bot_token = record.get("bot_token") or None
        self._base_url = resolve_base_url(record.get("baseurl"))
        scanner_id = str(record.get("ilink_user_id") or "").strip()

        self.default_chat_id = (
            getattr(settings, "WEIXIN_DEFAULT_USER_ID", "") or None
        )

        if allowed_user_ids is None:
            raw = getattr(settings, "WEIXIN_ALLOWED_USER_IDS", "") or ""
            allowed = {uid.strip() for uid in raw.split(",") if uid.strip()}
            if self.default_chat_id:
                allowed.add(self.default_chat_id)
            allowed_user_ids = allowed
        self._allow_all_legacy = False
        if not allowed_user_ids:
            if scanner_id:
                allowed_user_ids = {scanner_id}
                logger.info(
                    "WeixinGateway: WEIXIN_ALLOWED_USER_IDS empty — authorising "
                    "only the QR scanner (%s)", scanner_id,
                )
            else:
                self._allow_all_legacy = True
                logger.warning(
                    "WeixinGateway: no WEIXIN_ALLOWED_USER_IDS and the token "
                    "file at %s has no scanner id (ilink_user_id) — accepting "
                    "messages from ANY WeChat user who can reach this bot. "
                    "Set WEIXIN_ALLOWED_USER_IDS (or re-run "
                    "`python -m backend.weixin.qr_login`) to lock it down.",
                    token_path,
                )
        self.allowed_chat_ids = allowed_user_ids

        self._poller: WeixinPoller | None = None
        self.sender: WeixinSender | None = None
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self._bot_token:
            logger.warning(
                "WeixinGateway: no bot_token at %s — run "
                "`python -m backend.weixin.qr_login` and scan the ClawBot "
                "QR with WeChat. Skipping start.",
                self._token_path,
            )
            return

        self._poller = WeixinPoller(
            bot_token=self._bot_token,
            on_message=self._handle_message,
            allowed_user_ids=(None if self._allow_all_legacy else self.allowed_chat_ids),
            base_url=self._base_url,
        )
        self.sender = WeixinSender(
            bot_token=self._bot_token,
            default_user_id=self.default_chat_id,
            get_context_token=self._poller.latest_context_token,
            base_url=self._base_url,
        )
        await self.sender.start()
        await self._poller.start()
        self._poll_task = asyncio.create_task(
            self._poller.poll_loop(), name="weixin-poller",
        )
        logger.info("WeixinGateway started")

    async def stop(self) -> None:
        if self._poller:
            await self._poller.stop()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self.sender:
            await self.sender.stop()
        logger.info("WeixinGateway stopped")

    async def _handle_message(self, envelope: MessageEnvelope) -> None:
        logger.info(
            "WeChat message from %s: %s",
            envelope.sender_id,
            (envelope.content or "")[:80],
        )
        if self._on_user_message is None:
            logger.warning("WeixinGateway: no on_user_message handler configured")
            return
        try:
            await self._on_user_message(envelope)
        except Exception as exc:
            logger.error(
                "WeixinGateway on_user_message error: %s", exc, exc_info=True,
            )

    async def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        if self.sender is None:
            logger.warning("WeixinGateway.send_message: gateway not started")
            return False
        target = chat_id or self.default_chat_id
        if not target:
            logger.warning("WeixinGateway.send_message: no user_id available")
            return False
        return await self.sender.send_message(
            text=text, user_id=target, reply_markup=reply_markup,
        )

    async def send_photo(
        self,
        photo_path: str,
        caption: str = "",
        chat_id: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        if self.sender is None:
            logger.warning("WeixinGateway.send_photo: gateway not started")
            return False
        target = chat_id or self.default_chat_id
        if not target:
            logger.warning("WeixinGateway.send_photo: no user_id available")
            return False
        return await self.sender.send_photo(
            photo_path=photo_path,
            user_id=target,
        )

    async def edit_message(
        self,
        chat_id: str,
        message_id: Any,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        return await self.send_message(
            text=text, chat_id=chat_id, reply_markup=reply_markup,
        )

    async def answer_callback(
        self,
        callback_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> bool:
        return True

    def is_muted(self) -> bool:
        return self.sender.is_muted() if self.sender else False
