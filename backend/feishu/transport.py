from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from .models import MessageChannel, MessageEnvelope

try:
    from fastapi import Request as _FastAPIRequest
except ImportError:
    _FastAPIRequest = None

logger = logging.getLogger(__name__)


OnMessage = Callable[[MessageEnvelope], Awaitable[None]]
OnCardAction = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]

_MAX_TIMESTAMP_SKEW_S = 300
_WS_BACKOFF_MIN = 1.0
_WS_BACKOFF_MAX = 60.0
_WS_HEALTH_POLL_S = 5.0
_WS_DEAD_AFTER_S = 30.0


def _aes_cbc_decrypt(key: bytes, iv: bytes, body: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        pass
    else:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return decryptor.update(body) + decryptor.finalize()

    try:
        from Crypto.Cipher import AES
    except ImportError as exc:
        raise ValueError(
            "cannot decrypt Feishu payload: neither `cryptography` nor "
            "`pycryptodome` is installed"
        ) from exc
    return AES.new(key, AES.MODE_CBC, iv).decrypt(body)


def decrypt_feishu_payload(encrypt_key: str, ciphertext_b64: str) -> dict[str, Any]:
    if not encrypt_key:
        raise ValueError("encrypted payload received but no encrypt key configured")
    try:
        blob = base64.b64decode(ciphertext_b64)
    except Exception as exc:
        raise ValueError(f"encrypt field is not valid base64: {exc}") from exc
    if len(blob) < 32 or (len(blob) - 16) % 16:
        raise ValueError("encrypted payload has an invalid length")

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv, body = blob[:16], blob[16:]
    padded = _aes_cbc_decrypt(key, iv, body)

    pad = padded[-1] if padded else 0
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        raise ValueError("bad PKCS7 padding (wrong encrypt key?)")
    plain = padded[:-pad]

    parsed = json.loads(plain.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("decrypted payload is not a JSON object")
    return parsed


def _event_to_envelope(event: dict[str, Any]) -> MessageEnvelope | None:
    try:
        payload = event.get("event", {}) or {}
        message = payload.get("message", {}) or {}
        sender = payload.get("sender", {}) or {}
        sender_id_blob = sender.get("sender_id", {}) or {}

        msg_type = message.get("message_type") or message.get("msg_type") or "text"
        if msg_type != "text":
            logger.info("Feishu: ignoring non-text message_type=%s", msg_type)
            return None

        raw_content = message.get("content", "{}")
        try:
            content = json.loads(raw_content).get("text", "")
        except (ValueError, TypeError):
            content = str(raw_content)

        chat_id = message.get("chat_id") or ""
        sender_id = (
            sender_id_blob.get("open_id")
            or sender_id_blob.get("user_id")
            or sender_id_blob.get("union_id")
            or "unknown"
        )
        platform_msg_id = message.get("message_id") or uuid.uuid4().hex

        timestamp = datetime.now(timezone.utc)
        try:
            create_time = message.get("create_time") or message.get("update_time")
            if create_time:
                timestamp = datetime.fromtimestamp(
                    int(create_time) / 1000.0, tz=timezone.utc,
                )
        except (TypeError, ValueError, OSError, OverflowError):
            pass

        return MessageEnvelope(
            message_id=platform_msg_id,
            channel=MessageChannel.FEISHU,
            sender_id=str(sender_id),
            content=str(content),
            timestamp=timestamp,
            chat_id=str(chat_id) if chat_id else None,
            conversation_id=str(chat_id) if chat_id else None,
            platform_message_id=str(platform_msg_id),
            metadata={
                "tenant_key": event.get("header", {}).get("tenant_key", ""),
                "event_id": event.get("header", {}).get("event_id", ""),
            },
        )
    except Exception as exc:
        logger.warning("Feishu: failed to parse message event: %s", exc)
        return None


def _is_allowed(envelope: MessageEnvelope, allowed: set[str] | None) -> bool:
    if not allowed:
        return False
    return (envelope.chat_id or "") in allowed


class FeishuWebhookTransport:

    def __init__(
        self,
        on_message: OnMessage,
        on_card_action: OnCardAction,
        webhook_path: str,
        verification_token: str,
        encrypt_key: str = "",
        allowed_chat_ids: set[str] | None = None,
    ) -> None:
        self._on_message = on_message
        self._on_card_action = on_card_action
        self._webhook_path = webhook_path
        self._verification_token = verification_token
        self._encrypt_key = encrypt_key
        self._allowed_chat_ids = allowed_chat_ids or set()
        self._registered = False
        self._seen_event_ids: dict[str, None] = {}
        self._MAX_SEEN = 500
        self._bg_tasks: set[asyncio.Task[Any]] = set()


    async def start(self) -> None:
        if not self._registered:
            logger.warning(
                "FeishuWebhookTransport.start() called before register_routes(); "
                "webhook will be inert until mounted on a FastAPI app."
            )

    async def stop(self) -> None:
        return None


    def verify_signature(
        self,
        timestamp: str,
        nonce: str,
        raw_body: bytes,
        signature: str,
    ) -> bool:
        if not self._encrypt_key:
            return True
        if not signature:
            return False
        try:
            digest_src = (
                (timestamp or "")
                + (nonce or "")
                + self._encrypt_key
            ).encode("utf-8") + raw_body
            expected = hashlib.sha256(digest_src).hexdigest()
            return hmac.compare_digest(expected, signature)
        except Exception as exc:
            logger.warning("Feishu webhook signature check failed: %s", exc)
            return False

    def verify_timestamp(self, timestamp: str) -> bool:
        try:
            sent_at = float(timestamp)
        except (TypeError, ValueError):
            return False
        return abs(time.time() - sent_at) <= _MAX_TIMESTAMP_SKEW_S

    def verify_body_token(self, body: dict[str, Any]) -> bool:
        if not self._verification_token:
            return True
        token = (
            (body.get("header") or {}).get("token")
            or body.get("token")
            or ""
        )
        if not token:
            return not (body.get("header") or {}).get("event_type")
        return hmac.compare_digest(str(token), self._verification_token)


    def register_routes(self, app: Any) -> None:
        from fastapi.responses import JSONResponse

        if _FastAPIRequest is None:
            raise RuntimeError(
                "FastAPI is required to register the Feishu webhook transport."
            )

        transport = self

        async def _handle_webhook(request: _FastAPIRequest):
            raw = await request.body()
            timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
            nonce = request.headers.get("X-Lark-Request-Nonce", "")
            signature = request.headers.get("X-Lark-Signature", "")

            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                return JSONResponse({"code": 400, "msg": "bad json"}, status_code=400)

            logger.debug(
                "Feishu webhook POST: keys=%s sig=%s",
                list(body.keys()), bool(signature),
            )

            if isinstance(body.get("encrypt"), str):
                try:
                    body = transport.decrypt_body(body["encrypt"])
                except ValueError as exc:
                    logger.warning("Feishu webhook: cannot decrypt payload: %s", exc)
                    return JSONResponse(
                        {"code": 400, "msg": "bad encrypt"}, status_code=400,
                    )

            if body.get("type") == "url_verification":
                token = body.get("token", "")
                if (
                    token
                    and transport._verification_token
                    and not hmac.compare_digest(str(token), transport._verification_token)
                ):
                    return JSONResponse({"code": 401, "msg": "bad token"}, status_code=401)
                return {"challenge": body.get("challenge", "")}

            if transport._encrypt_key:
                if not transport.verify_signature(timestamp, nonce, raw, signature):
                    logger.warning(
                        "Feishu webhook: bad sha256 signature, rejecting",
                    )
                    return JSONResponse(
                        {"code": 401, "msg": "bad signature"}, status_code=401,
                    )
                if not transport.verify_timestamp(timestamp):
                    logger.warning(
                        "Feishu webhook: stale/invalid timestamp %r, rejecting",
                        timestamp,
                    )
                    return JSONResponse(
                        {"code": 401, "msg": "stale request"}, status_code=401,
                    )
            if transport._verification_token:
                if not transport.verify_body_token(body):
                    logger.warning(
                        "Feishu webhook: body token mismatch, rejecting",
                    )
                    return JSONResponse(
                        {"code": 401, "msg": "bad token"}, status_code=401,
                    )

            if (body.get("header") or {}).get("event_type") == "im.message.receive_v1":
                transport.dispatch_in_background(body)
                return {"code": 0, "msg": "ok"}

            result = await transport.dispatch_event(body)
            if isinstance(result, dict):
                return result
            return {"code": 0, "msg": "ok"}

        app.post(self._webhook_path, name="feishu_webhook")(_handle_webhook)
        self._registered = True
        logger.info("FeishuWebhookTransport: mounted at %s", self._webhook_path)


    def decrypt_body(self, ciphertext_b64: str) -> dict[str, Any]:
        return decrypt_feishu_payload(self._encrypt_key, ciphertext_b64)

    def _is_duplicate(self, event_id: str) -> bool:
        if not event_id:
            return False
        if event_id in self._seen_event_ids:
            return True
        self._seen_event_ids[event_id] = None
        if len(self._seen_event_ids) > self._MAX_SEEN:
            for old in list(self._seen_event_ids)[: self._MAX_SEEN // 2]:
                del self._seen_event_ids[old]
        return False

    def dispatch_in_background(self, event: dict[str, Any]) -> None:
        task = asyncio.create_task(self._dispatch_guarded(event))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _dispatch_guarded(self, event: dict[str, Any]) -> None:
        try:
            await self.dispatch_event(event)
        except Exception as exc:
            logger.error(
                "Feishu webhook: background dispatch failed: %s", exc, exc_info=True,
            )

    async def dispatch_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        header = event.get("header", {}) or {}
        event_type = header.get("event_type", "")

        if event_type == "im.message.receive_v1":
            if self._is_duplicate(str(header.get("event_id") or "")):
                logger.info(
                    "Feishu webhook: dropping duplicate event_id=%s",
                    header.get("event_id"),
                )
                return None
            envelope = _event_to_envelope(event)
            if envelope is None:
                return None
            if not _is_allowed(envelope, self._allowed_chat_ids):
                logger.warning(
                    "Feishu webhook: default-deny rejected chat_id=%s",
                    envelope.chat_id,
                )
                return None
            await self._on_message(envelope)
            return None

        if event_type == "card.action.trigger":
            return await self._on_card_action(event)

        if "action" in event and (
            "open_message_id" in event
            or "token" in event
            or "open_id" in event
            or "context" in event
            or "host" in event
        ):
            wrapped = {
                "header": {"event_type": "card.action.trigger"},
                "event": event,
            }
            new_card = await self._on_card_action(wrapped)
            if isinstance(new_card, dict):
                return {"card": new_card}
            return None

        logger.info(
            "Feishu webhook: ignoring unknown payload (event_type=%r, keys=%s)",
            event_type, list(event.keys()),
        )
        return None


class FeishuWsTransport:

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: OnMessage,
        on_card_action: OnCardAction,
        allowed_chat_ids: set[str] | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._on_message = on_message
        self._on_card_action = on_card_action
        self._allowed_chat_ids = allowed_chat_ids or set()
        self._client: Any = None
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._seen_ids: dict[str, None] = {}
        self._MAX_SEEN = 500
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    def _is_duplicate(self, key: str) -> bool:
        if not key:
            return False
        if key in self._seen_ids:
            return True
        self._seen_ids[key] = None
        if len(self._seen_ids) > self._MAX_SEEN:
            for old in list(self._seen_ids)[: self._MAX_SEEN // 2]:
                del self._seen_ids[old]
        return False

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error(
                    "Feishu ws: background handler failed: %s", t.exception(),
                    exc_info=t.exception(),
                )

        task.add_done_callback(_done)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="feishu-ws")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._client = None

    async def _run(self) -> None:
        try:
            import lark_oapi as lark
        except ImportError:
            logger.error(
                "FeishuWsTransport: lark_oapi not installed — transport disabled"
            )
            return

        backoff = _WS_BACKOFF_MIN
        while True:
            self._connected = False
            try:
                await self._connect_and_serve(lark)
                logger.warning("FeishuWsTransport: connection ended, reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "FeishuWsTransport error (reconnect in ~%.0fs): %s",
                    backoff, exc, exc_info=True,
                )
            if self._connected:
                backoff = _WS_BACKOFF_MIN
            await asyncio.sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, _WS_BACKOFF_MAX)

    async def _connect_and_serve(self, lark: Any) -> None:
        ping_task: asyncio.Task[Any] | None = None
        try:
            event_handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._on_sdk_message)
                .register_p2_card_action_trigger(self._on_sdk_card_action)
                .build()
            )
            kwargs: dict[str, Any] = {
                "event_handler": event_handler,
                "log_level": lark.LogLevel.WARNING,
                "auto_reconnect": False,
            }
            try:
                self._client = lark.ws.Client(
                    self._app_id, self._app_secret, **kwargs,
                )
            except TypeError:
                kwargs.pop("auto_reconnect")
                self._client = lark.ws.Client(
                    self._app_id, self._app_secret, **kwargs,
                )
            await self._client._connect()
            self._connected = True
            ping_task = asyncio.create_task(
                self._client._ping_loop(), name="feishu-ws-ping",
            )
            await self._watch_link()
        finally:
            if ping_task is not None:
                ping_task.cancel()
                try:
                    await ping_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._client is not None:
                try:
                    await asyncio.wait_for(self._client._disconnect(), timeout=10)
                except Exception:
                    pass

    async def _watch_link(self) -> None:
        down_for = 0.0
        while True:
            await asyncio.sleep(_WS_HEALTH_POLL_S)
            client = self._client
            if client is None:
                return
            if getattr(client, "_conn", None) is None:
                down_for += _WS_HEALTH_POLL_S
                if down_for >= _WS_DEAD_AFTER_S:
                    logger.warning(
                        "FeishuWsTransport: websocket down for %.0fs — rebuilding",
                        down_for,
                    )
                    return
            else:
                down_for = 0.0


    def _on_sdk_message(self, data: Any) -> None:
        try:
            event = _sdk_event_to_dict(data, "im.message.receive_v1")
            envelope = _event_to_envelope(event)
            if envelope is None:
                return
            if not _is_allowed(envelope, self._allowed_chat_ids):
                logger.warning(
                    "Feishu ws: default-deny rejected chat_id=%s",
                    envelope.chat_id,
                )
                return
            event_id = str((event.get("header") or {}).get("event_id") or "")
            msg_id = str(envelope.platform_message_id or "")
            if self._is_duplicate(event_id) or self._is_duplicate(msg_id):
                logger.info(
                    "Feishu ws: dropping duplicate event_id=%s message_id=%s",
                    event_id, msg_id,
                )
                return
            self._spawn(self._on_message(envelope))
        except Exception as exc:
            logger.error("Feishu ws message handler error: %s", exc, exc_info=True)

    def _on_sdk_card_action(self, data: Any) -> None:
        try:
            event = _sdk_event_to_dict(data, "card.action.trigger")
            self._spawn(self._on_card_action(event))
        except Exception as exc:
            logger.error("Feishu ws card handler error: %s", exc, exc_info=True)


def _obj_to_dict(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _obj_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_obj_to_dict(v) for v in obj]
    inst_dict = getattr(obj, "__dict__", None)
    if isinstance(inst_dict, dict) and inst_dict:
        return {
            k: _obj_to_dict(v)
            for k, v in inst_dict.items()
            if not k.startswith("_")
        }
    return repr(obj)


def _sdk_event_to_dict(data: Any, event_type: str) -> dict[str, Any]:
    def _ensure_header(d: dict[str, Any]) -> dict[str, Any]:
        header = d.get("header")
        if not isinstance(header, dict):
            header = {}
            d["header"] = header
        header.setdefault("event_type", event_type)
        if not isinstance(d.get("event"), dict):
            d["event"] = {}
        return d

    if data is None:
        return {"header": {"event_type": event_type}, "event": {}}
    if isinstance(data, dict):
        return _ensure_header(data)
    if hasattr(data, "to_dict"):
        try:
            raw = data.to_dict()
            if isinstance(raw, dict):
                return _ensure_header(raw)
        except Exception:
            pass
    walked = _obj_to_dict(data)
    if isinstance(walked, dict):
        return _ensure_header(walked)
    return {"header": {"event_type": event_type}, "event": {"_raw": repr(data)}}


def make_transport(
    settings: Any,
    on_message: OnMessage,
    on_card_action: OnCardAction,
    allowed_chat_ids: set[str] | None = None,
):
    transport_kind = (getattr(settings, "FEISHU_TRANSPORT", "ws") or "ws").lower()
    if transport_kind == "webhook":
        return FeishuWebhookTransport(
            on_message=on_message,
            on_card_action=on_card_action,
            webhook_path=getattr(settings, "FEISHU_WEBHOOK_PATH", "/api/feishu/webhook"),
            verification_token=getattr(settings, "FEISHU_VERIFICATION_TOKEN", ""),
            encrypt_key=getattr(settings, "FEISHU_ENCRYPT_KEY", ""),
            allowed_chat_ids=allowed_chat_ids,
        )
    if transport_kind == "ws":
        return FeishuWsTransport(
            app_id=getattr(settings, "FEISHU_APP_ID", ""),
            app_secret=getattr(settings, "FEISHU_APP_SECRET", ""),
            on_message=on_message,
            on_card_action=on_card_action,
            allowed_chat_ids=allowed_chat_ids,
        )
    raise ValueError(
        f"Unknown FEISHU_TRANSPORT={transport_kind!r} (expected 'ws' or 'webhook')"
    )
