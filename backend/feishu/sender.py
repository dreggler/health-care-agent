from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .cards import build_evidence_card, build_plain_card

logger = logging.getLogger(__name__)


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BULLET_RE = re.compile(r"^[\t ]*[-*] +(.*)$", re.MULTILINE)


def markdown_to_feishu_post(text: str) -> str:
    if not text:
        return ""
    try:
        out = text

        def _fence_sub(match: re.Match[str]) -> str:
            body = match.group(2)
            lines = body.splitlines()
            return "\n".join(f"    {line}" for line in lines)

        out = _CODE_FENCE_RE.sub(_fence_sub, out)

        out = _BULLET_RE.sub(r"• \1", out)

        out = _BOLD_RE.sub(r"**\1**", out)
        out = _INLINE_CODE_RE.sub(r"`\1`", out)
        out = _LINK_RE.sub(r"[\1](\2)", out)

        return out
    except Exception as exc:
        logger.warning("markdown_to_feishu_post failed, falling back to plain: %s", exc)
        return text


class FeishuSender:

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        default_chat_id: str | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self.default_chat_id = default_chat_id
        self._client: Any = None
        self._muted = False


    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import lark_oapi as lark
        except ImportError as exc:
            raise RuntimeError(
                "lark_oapi is not installed. Add 'lark-oapi>=1.4.0' to "
                "backend/requirements.txt and reinstall."
            ) from exc
        self._client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .build()
        )
        return self._client

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self._client = None

    def is_muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)


    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        target = chat_id or self.default_chat_id
        if not target:
            logger.warning("FeishuSender.send_message: no chat_id / default set")
            return False

        if reply_markup is not None:
            card = _reply_markup_to_card(text, reply_markup)
            return await self.send_card(target, card)

        body = markdown_to_feishu_post(text)
        return await self._send_text(target, body)

    async def send_photo(
        self,
        chat_id: str,
        image_path: str,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        target = chat_id or self.default_chat_id
        if not target:
            logger.warning("FeishuSender.send_photo: no chat_id / default set")
            return False

        try:
            image_key = await asyncio.to_thread(self._upload_image_sync, image_path)
        except Exception as exc:
            logger.error("Feishu image upload failed: %s", exc)
            return False
        if not image_key:
            return False

        elements: list[dict[str, Any]] = []
        if caption:
            elements.append(
                {"tag": "div", "text": {"tag": "lark_md", "content": markdown_to_feishu_post(caption)}}
            )
        elements.append({"tag": "img", "img_key": image_key, "alt": {"tag": "plain_text", "content": caption or "image"}})

        if reply_markup is not None:
            card = _reply_markup_to_card("", reply_markup)
            card_elements = card.get("elements", [])
            card["elements"] = elements + card_elements
            return await self.send_card(target, card)

        card = {"config": {"wide_screen_mode": True}, "elements": elements}
        return await self.send_card(target, card)

    async def send_card(self, chat_id: str, card: dict[str, Any]) -> bool:
        target = chat_id or self.default_chat_id
        if not target:
            return False
        return await asyncio.to_thread(self._send_card_sync, target, card)

    async def edit_message(
        self,
        message_id: str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        card: dict[str, Any] | None = None,
    ) -> bool:
        if card is None:
            if reply_markup is not None:
                card = _reply_markup_to_card(text, reply_markup)
            else:
                card = build_plain_card(markdown_to_feishu_post(text))
        return await asyncio.to_thread(self._patch_card_sync, message_id, card)

    async def answer_callback(
        self,
        callback_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> bool:
        return True


    def _send_text_sync(self, chat_id: str, text: str) -> bool:
        try:
            import json as _json

            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            client = self._ensure_client()
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(_json.dumps({"text": text}, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            resp = client.im.v1.message.create(req)
            if not resp.success():
                logger.warning(
                    "Feishu send_text failed: code=%s msg=%s", resp.code, resp.msg
                )
                return False
            return True
        except Exception as exc:
            logger.error("Feishu _send_text_sync error: %s", exc, exc_info=True)
            return False

    async def _send_text(self, chat_id: str, text: str) -> bool:
        return await asyncio.to_thread(self._send_text_sync, chat_id, text)

    def _send_card_sync(self, chat_id: str, card: dict[str, Any]) -> bool:
        try:
            import json as _json

            import lark_oapi as lark
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            client = self._ensure_client()
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(_json.dumps(card, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            resp = client.im.v1.message.create(req)
            if not resp.success():
                logger.warning(
                    "Feishu send_card failed: code=%s msg=%s", resp.code, resp.msg
                )
                return False
            return True
        except Exception as exc:
            logger.error("Feishu _send_card_sync error: %s", exc, exc_info=True)
            return False

    def _patch_card_sync(self, message_id: str, card: dict[str, Any]) -> bool:
        try:
            import json as _json

            from lark_oapi.api.im.v1 import (
                PatchMessageRequest,
                PatchMessageRequestBody,
            )

            client = self._ensure_client()
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(_json.dumps(card, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            resp = client.im.v1.message.patch(req)
            if not resp.success():
                logger.warning(
                    "Feishu patch_card failed: code=%s msg=%s", resp.code, resp.msg
                )
                return False
            return True
        except Exception as exc:
            logger.error("Feishu _patch_card_sync error: %s", exc, exc_info=True)
            return False

    def _upload_image_sync(self, image_path: str) -> str | None:
        try:
            from lark_oapi.api.im.v1 import (
                CreateImageRequest,
                CreateImageRequestBody,
            )

            client = self._ensure_client()
            with open(image_path, "rb") as fh:
                req = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(fh)
                        .build()
                    )
                    .build()
                )
                resp = client.im.v1.image.create(req)
            if not resp.success():
                logger.warning(
                    "Feishu upload_image failed: code=%s msg=%s",
                    resp.code,
                    resp.msg,
                )
                return None
            return getattr(resp.data, "image_key", None)
        except Exception as exc:
            logger.error("Feishu _upload_image_sync error: %s", exc, exc_info=True)
            return None


def _reply_markup_to_card(text: str, reply_markup: dict[str, Any]) -> dict[str, Any]:
    try:
        keyboard = reply_markup.get("inline_keyboard") or []
        for row in keyboard:
            for btn in row:
                data = str(btn.get("callback_data", ""))
                if ":" not in data:
                    continue
                action, msg_hash = data.split(":", 1)
                if action in ("evidence", "restore"):
                    return build_evidence_card(
                        markdown_to_feishu_post(text), msg_hash,
                    )
    except Exception as exc:
        logger.warning("reply_markup_to_card fallback: %s", exc)
    return build_plain_card(markdown_to_feishu_post(text))
