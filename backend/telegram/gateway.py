from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from ..i18n import t
from ..messaging.base import BaseGateway, MessageChannel
from .command_parser import CommandParser
from .handlers.command_handler import CommandHandler
from .models import CommandType, MessageEnvelope
from .poller import TelegramPoller
from .sender import TelegramSender

logger = logging.getLogger(__name__)


class TelegramGateway(BaseGateway):

    channel = MessageChannel.TELEGRAM

    def __init__(
        self,
        token: str,
        default_chat_id: str | None = None,
        poll_timeout: int = 30,
        allowed_chat_ids: set[str] | None = None,
        get_active_agents: Callable | None = None,
        get_memory_manager: Callable | None = None,
        on_user_message: Callable | None = None,
    ) -> None:
        self._token = token
        self._default_chat_id = default_chat_id
        self.default_chat_id = default_chat_id
        self.allowed_chat_ids = allowed_chat_ids

        self.sender = TelegramSender(token, default_chat_id)
        self._parser = CommandParser()
        self._poller = TelegramPoller(
            token=token,
            on_message=self._handle_message,
            poll_timeout=poll_timeout,
            allowed_chat_ids=allowed_chat_ids,
            on_callback_query=self._handle_callback_query,
        )
        self._command_handler = CommandHandler(
            get_active_agents=get_active_agents or (lambda: {}),
            get_memory_manager=get_memory_manager or (lambda pid: None),
            get_telegram_sender=lambda: self.sender,
        )

        self._on_user_message = on_user_message
        self._get_memory_manager = get_memory_manager or (lambda pid: None)

        self._fact_verifiers: dict = {}

        self._poll_task: asyncio.Task | None = None


    async def start(self) -> None:
        await self.sender.start()
        await self._poller.start()
        self._poll_task = asyncio.create_task(
            self._poller.poll_loop(), name="telegram-poller"
        )
        logger.info("TelegramGateway started")

    async def stop(self) -> None:
        await self._poller.stop()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        await self.sender.stop()
        logger.info("TelegramGateway stopped")


    async def _handle_message(self, envelope: MessageEnvelope) -> None:
        cmd = self._parser.parse(envelope.content, envelope=envelope)
        chat_id = envelope.chat_id

        logger.info(
            "TG message from %s (chat=%s): type=%s text=%s",
            envelope.sender_id,
            chat_id,
            cmd.command_type.value,
            envelope.content[:80],
        )

        if cmd.command_type == CommandType.ASK:
            if self._on_user_message:
                await self._on_user_message(envelope)
            else:
                logger.warning("Received ASK command but no _on_user_message handler is configured.")
            return

        if cmd.command_type == CommandType.UNKNOWN:
            reply = await self._command_handler.handle(cmd)
            await self.sender.send_message(
                reply, chat_id=chat_id,
                reply_to_message_id=envelope.telegram_message_id,
            )
            return

        reply = await self._command_handler.handle(cmd)
        await self.sender.send_message(
            reply,
            chat_id=chat_id,
            reply_to_message_id=envelope.telegram_message_id,
        )


    async def _handle_callback_query(self, callback_query: dict) -> None:
        query_id = callback_query.get("id", "")
        data = callback_query.get("data", "")
        message = callback_query.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        message_id = message.get("message_id")

        if not message_id:
            await self.sender.answer_callback_query(
                query_id, t("callback.cannot_edit_message"),
            )
            return

        try:
            if data.startswith("evidence:"):
                msg_hash = data.split(":", 1)[1]
                await self._show_evidence(query_id, chat_id, message_id, msg_hash)
            elif data.startswith("restore:"):
                msg_hash = data.split(":", 1)[1]
                await self._restore_original(query_id, chat_id, message_id, msg_hash)
            else:
                await self.sender.answer_callback_query(
                    query_id, t("callback.unknown_action"),
                )
        except Exception as exc:
            logger.error("Callback query handler error: %s", exc, exc_info=True)
            try:
                await self.sender.answer_callback_query(
                    query_id, text=t("callback.error_retrieving_evidence"),
                )
            except Exception:
                pass

    def _get_fact_verifier(self, user_id: str = "LiveUser"):
        if user_id not in self._fact_verifiers:
            from ..agent.fact_verifier import FactVerifier
            from ..config import settings
            self._fact_verifiers[user_id] = FactVerifier(
                settings.MEMORY_DB_PATH, user_id,
            )
        return self._fact_verifiers[user_id]

    async def _show_evidence(
        self, query_id: str, chat_id: str, message_id: int, msg_hash: str,
    ) -> None:
        verifier = self._get_fact_verifier()
        evidence = await asyncio.to_thread(verifier.get_evidence, msg_hash)

        if not evidence:
            await self.sender.answer_callback_query(
                query_id, t("callback.evidence_not_found"), show_alert=True,
            )
            return

        evidence_text = await asyncio.to_thread(verifier.format_evidence_for_display, evidence)

        reply_markup = {
            "inline_keyboard": [[
                {"text": "\u21a9\ufe0f Back", "callback_data": f"restore:{msg_hash}"}
            ]]
        }

        ok = await self.sender.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=evidence_text,
            reply_markup=reply_markup,
        )

        if ok:
            await self.sender.answer_callback_query(query_id)
        else:
            await self.sender.answer_callback_query(
                query_id, t("callback.show_evidence_failed"), show_alert=True,
            )

    async def _restore_original(
        self, query_id: str, chat_id: str, message_id: int, msg_hash: str,
    ) -> None:
        verifier = self._get_fact_verifier()
        evidence = await asyncio.to_thread(verifier.get_evidence, msg_hash)

        if not evidence:
            await self.sender.answer_callback_query(
                query_id, t("callback.original_message_not_found"), show_alert=True,
            )
            return

        original_text = evidence.get("message_text", "")
        if not original_text:
            await self.sender.answer_callback_query(
                query_id, t("callback.original_text_unavailable"), show_alert=True,
            )
            return

        reply_markup = {
            "inline_keyboard": [[
                {
                    "text": "\U0001f4ca Show Evidence",
                    "callback_data": f"evidence:{msg_hash}",
                }
            ]]
        }

        ok = await self.sender.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=original_text,
            reply_markup=reply_markup,
        )

        if ok:
            await self.sender.answer_callback_query(query_id)
        else:
            await self.sender.answer_callback_query(
                query_id, t("callback.restore_failed"), show_alert=True,
            )


    async def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        return await self.sender.send_message(
            text=text,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
        )

    async def send_photo(
        self,
        photo_path: str,
        caption: str = "",
        chat_id: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        return await self.sender.send_photo(
            photo_path=photo_path,
            caption=caption,
            chat_id=chat_id,
            reply_markup=reply_markup,
        )

    async def edit_message(
        self,
        chat_id: str,
        message_id: Any,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        return await self.sender.edit_message_text(
            chat_id=chat_id,
            message_id=int(message_id),
            text=text,
            reply_markup=reply_markup,
        )

    async def answer_callback(
        self,
        callback_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> bool:
        return await self.sender.answer_callback_query(
            callback_query_id=callback_id,
            text=text,
            show_alert=show_alert,
        )

    def is_muted(self) -> bool:
        return self.sender.is_muted()
