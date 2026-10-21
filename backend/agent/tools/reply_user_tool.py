from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...i18n import t
from ...messaging.base import BaseGateway, MessageEnvelope
from ...messaging.registry import GatewayRegistry
from .base import BaseTool, _FlowVar
from .paths import ChartPathError, resolve_chart_path

logger = logging.getLogger(__name__)


class ReplyUserTool(BaseTool):

    name = "reply_user"

    _current_envelope: MessageEnvelope | None = _FlowVar(None)

    def __init__(
        self,
        gateway_registry: GatewayRegistry,
        default_chat_id: str | None = None,
        fact_verifier=None,
    ) -> None:
        self._gateway_registry = gateway_registry
        self._default_chat_id = default_chat_id
        self._fact_verifier = fact_verifier
        self._llm_provider = None
        self._current_tool_results: list = []
        self._current_chat_history: list = []
        self._current_envelope: MessageEnvelope | None = None


    def get_definition(self) -> dict[str, Any]:
        return self._get_definition_from_json("reply_user")


    def _resolve_gateway(self) -> BaseGateway | None:
        if self._current_envelope is not None:
            gw = self._gateway_registry.for_envelope(self._current_envelope)
            if gw is not None:
                return gw
        for gw in self._gateway_registry.all():
            if getattr(gw, "default_chat_id", None):
                return gw
        gws = self._gateway_registry.all()
        return gws[0] if gws else None


    async def execute(
        self,
        message: str,
        chat_id: str = "",
        image_path: str = "",
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        gateway = self._resolve_gateway()
        if gateway is None:
            return {
                "success": False,
                "error": t("gateway.no_gateway_available"),
            }

        target = chat_id
        if not target and self._current_envelope is not None:
            target = self._current_envelope.chat_id or ""
        if not target:
            target = getattr(gateway, "default_chat_id", None) or self._default_chat_id or ""
        if not target:
            return {
                "success": False,
                "error": t("gateway.no_chat_id"),
            }

        verification = await self._verify_and_build_markup(message)
        if verification["status"] in ("fabricated", "unverified"):
            logger.warning(
                "reply_user blocked (%s): %s",
                verification["status"], verification["detail"],
            )
            return {
                "success": False,
                "error": t(
                    "reply.fact_blocked",
                    status=verification["status"],
                    detail=verification["detail"],
                ),
                "blocked": "verification",
                "verification_detail": str(verification["detail"] or "")[:200],
            }
        reply_markup = verification["reply_markup"]

        sent_image_id: str | None = None
        image_note = ""
        safe_image: str | None = None
        if image_path:
            try:
                safe_image = str(await asyncio.to_thread(resolve_chart_path, image_path))
            except ChartPathError as exc:
                logger.warning("reply_user: rejected image_path %r: %s", image_path, exc)
        if image_path:
            if safe_image is not None:
                photo_ok = await gateway.send_photo(
                    photo_path=safe_image,
                    caption=message[:1024] if message else "",
                    chat_id=target,
                    reply_markup=reply_markup,
                )
                if photo_ok:
                    logger.info(
                        "reply_user photo sent via %s to %s",
                        gateway.channel.value, target,
                    )
                    sent_image_id = getattr(gateway, "last_image_id", None) or None
                    if (
                        getattr(gateway, "supports_inline_caption", True)
                        and len(message) <= 1024
                    ):
                        return {
                            "success": True,
                            "message": "Photo with caption sent.",
                            "image_id": sent_image_id,
                        }
                else:
                    logger.warning("Photo send failed, falling back to text-only")
            else:
                image_note = (
                    "image_path was rejected: it must be an existing PNG/JPEG/GIF/WebP "
                    "file saved under /tmp (e.g. plt.savefig('/tmp/chart.png')). "
                    "The text was sent without the image."
                )

        ok = await gateway.send_message(
            text=message,
            chat_id=target,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
        )
        if ok:
            logger.info(
                "reply_user sent via %s to %s: %s",
                gateway.channel.value, target, message[:80],
            )
            result: dict[str, Any] = {
                "success": True,
                "message": "Reply sent successfully.",
                "image_id": sent_image_id,
            }
            if image_note:
                result["image_warning"] = image_note
            return result
        logger.warning(
            "reply_user failed on %s for chat %s (preview: %s)",
            gateway.channel.value, target, message[:100],
        )
        return {
            "success": False,
            "error": t("gateway.send_failed"),
        }
