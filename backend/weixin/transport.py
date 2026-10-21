from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .models import MessageChannel, MessageEnvelope
from .qr_login import CHANNEL_VERSION, common_headers

logger = logging.getLogger(__name__)

ILINK_BASE = "https://ilinkai.weixin.qq.com"
_LONG_POLL_HTTP_TIMEOUT = 50.0

_STATE_FILENAME = "weixin_poller_state.json"

OnMessage = Callable[[MessageEnvelope], Awaitable[None]]


def _resolve_state_path(explicit: Path | str | None) -> Path | None:
    if explicit is not None:
        return Path(explicit)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        from ..config import settings
        return Path(settings.MEMORY_DB_PATH) / _STATE_FILENAME
    except Exception as exc:
        logger.warning("WeixinPoller: cursor persistence disabled: %s", exc)
        return None


class WeixinPoller:

    def __init__(
        self,
        bot_token: str,
        on_message: OnMessage,
        allowed_user_ids: set[str] | None = None,
        state_path: Path | str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._base_url = (base_url or ILINK_BASE).rstrip("/")
        self._token = bot_token
        self._on_message = on_message
        self._allowed = allowed_user_ids
        self._cursor: str = ""
        self._running = False
        self._client: httpx.AsyncClient | None = None
        self._seen_ids: dict[str, None] = {}
        self._MAX_SEEN = 500
        self._last_context: dict[str, str] = {}

        self._state_path = _resolve_state_path(state_path)
        self._state_key = hashlib.sha256(bot_token.encode("utf-8")).hexdigest()[:16]
        self._save_warned = False
        self._load_state()

    def _load_state(self) -> None:
        path = self._state_path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state file is not a JSON object")
            if data.get("token") != self._state_key:
                logger.info(
                    "WeixinPoller: %s belongs to a different bot_token — "
                    "starting from a clean cursor", path,
                )
                return
            cursor = data.get("cursor")
            if isinstance(cursor, str):
                self._cursor = cursor
            seen = data.get("seen_ids")
            if isinstance(seen, list):
                for msg_id in seen[-self._MAX_SEEN:]:
                    if isinstance(msg_id, str):
                        self._seen_ids[msg_id] = None
            ctx = data.get("context_tokens")
            if isinstance(ctx, dict):
                for uid, tok in ctx.items():
                    if isinstance(uid, str) and isinstance(tok, str) and tok:
                        self._last_context[uid] = tok
            logger.info(
                "WeixinPoller resumed from %s (%d seen ids)",
                path, len(self._seen_ids),
            )
        except Exception as exc:
            logger.warning(
                "WeixinPoller: ignoring unusable state file %s (%s) — "
                "continuing with an in-memory cursor", path, exc,
            )

    def _snapshot_state(self) -> dict[str, Any]:
        return {
            "token": self._state_key,
            "cursor": self._cursor,
            "seen_ids": list(self._seen_ids)[-self._MAX_SEEN:],
            "context_tokens": dict(list(self._last_context.items())[-20:]),
        }

    def _save_state(self) -> None:
        self._write_state(self._snapshot_state())

    async def _asave_state(self) -> None:
        if self._state_path is None:
            return
        await asyncio.to_thread(self._write_state, self._snapshot_state())

    def _write_state(self, payload: dict[str, Any]) -> None:
        path = self._state_path
        if path is None:
            return
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            if not self._save_warned:
                self._save_warned = True
                logger.warning(
                    "WeixinPoller: cannot persist cursor to %s: %s", path, exc,
                )

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(_LONG_POLL_HTTP_TIMEOUT)
        )
        logger.info("WeixinPoller started")

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("WeixinPoller stopped")

    def latest_context_token(self, user_id: str) -> str | None:
        return self._last_context.get(user_id)

    async def poll_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                updates = await self._fetch_updates()
                backoff = 1.0
                for upd in updates:
                    await self._process_update(upd)
            except asyncio.CancelledError:
                logger.info("WeixinPoller cancelled")
                break
            except Exception as exc:
                logger.warning(
                    "WeixinPoller error (retry in %.0fs): %s", backoff, exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _fetch_updates(self) -> list[dict[str, Any]]:
        if not self._client:
            return []
        body: dict[str, Any] = {
            "get_updates_buf": self._cursor,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }

        resp = await self._client.post(
            f"{self._base_url}/ilink/bot/getupdates",
            json=body,
            headers=common_headers(self._token),
        )
        if resp.status_code == 401:
            raise RuntimeError(
                "iLink: 401 Unauthorized — bot_token expired. "
                "Re-run `python -m backend.weixin.qr_login`."
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"iLink getupdates HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        ret = data.get("ret")
        if ret not in (None, 0):
            raise RuntimeError(
                f"iLink getupdates app error: ret={ret} "
                f"err_msg={data.get('err_msg', '')!r}"
            )
        nxt = data.get("get_updates_buf") or ""
        if nxt and nxt != self._cursor:
            self._cursor = nxt
            await self._asave_state()
        return data.get("msgs") or []

    async def _process_update(self, update: dict[str, Any]) -> None:
        msg_id = str(update.get("message_id") or update.get("id") or "")
        if msg_id and msg_id in self._seen_ids:
            return
        if msg_id:
            self._seen_ids[msg_id] = None
            if len(self._seen_ids) > self._MAX_SEEN:
                for old in list(self._seen_ids)[: self._MAX_SEEN // 2]:
                    del self._seen_ids[old]
            await self._asave_state()

        from_user = str(update.get("from_user_id") or "")
        if not from_user:
            return

        if self._allowed is not None and from_user not in self._allowed:
            logger.warning(
                "WeChat: rejecting message from non-whitelisted user %s "
                "(set WEIXIN_ALLOWED_USER_IDS to allow)",
                from_user,
            )
            return

        ctx_token = str(update.get("context_token") or "")
        if ctx_token and self._last_context.get(from_user) != ctx_token:
            self._last_context[from_user] = ctx_token
            await self._asave_state()

        text = _extract_text(update)
        if not text:
            return

        ts_raw = update.get("timestamp") or update.get("ts")
        if ts_raw:
            try:
                ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                ts = datetime.now(timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        envelope = MessageEnvelope(
            message_id=msg_id or f"weixin:{ts.timestamp()}",
            channel=MessageChannel.WEIXIN,
            sender_id=from_user,
            content=text,
            timestamp=ts,
            chat_id=from_user,
            conversation_id=from_user,
            platform_message_id=msg_id,
            metadata={
                "context_token": ctx_token,
                "raw": update,
            },
        )

        try:
            await self._on_message(envelope)
        except Exception as exc:
            logger.error(
                "WeixinPoller dispatch error: %s", exc, exc_info=True,
            )


def _extract_text(update: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in update.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        text_item = item.get("text_item")
        if isinstance(text_item, dict):
            text = text_item.get("text") or ""
            if text:
                parts.append(text)
            continue
        logger.info(
            "WeChat: skipping non-text item type=%s keys=%s",
            item.get("type"), list(item.keys()),
        )
    return "\n".join(parts).strip()
