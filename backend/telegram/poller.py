from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .models import MessageChannel, MessageEnvelope

logger = logging.getLogger(__name__)

_STATE_FILENAME = "telegram_poller_state.json"


def _resolve_state_path(explicit: Path | str | None) -> Path | None:
    if explicit is not None:
        return Path(explicit)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        from ..config import settings
        return Path(settings.MEMORY_DB_PATH) / _STATE_FILENAME
    except Exception as exc:
        logger.warning("TelegramPoller: cursor persistence disabled: %s", exc)
        return None


class _RateLimited(RuntimeError):

    def __init__(self, retry_after: float, payload: Any) -> None:
        super().__init__(f"Telegram API rate limited (retry_after={retry_after}): {payload}")
        self.retry_after = retry_after


class TelegramPoller:

    def __init__(
        self,
        token: str,
        on_message: Callable[[MessageEnvelope], Coroutine],
        poll_timeout: int = 30,
        allowed_chat_ids: set | None = None,
        on_callback_query: Callable | None = None,
        state_path: Path | str | None = None,
    ) -> None:
        self._token = token
        self._on_message = on_message
        self._on_callback_query = on_callback_query
        self._poll_timeout = max(1, int(poll_timeout))
        self._allowed_chat_ids = allowed_chat_ids
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._offset: int = 0
        self._running = False
        self._client: httpx.AsyncClient | None = None
        self._seen_ids: dict[str, None] = {}
        self._MAX_SEEN = 500

        self._state_path = _resolve_state_path(state_path)
        self._state_key = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
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
                    "TelegramPoller: %s belongs to a different bot token — "
                    "starting from a clean cursor", path,
                )
                return
            offset = data.get("offset")
            if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0:
                self._offset = offset
            seen = data.get("seen_ids")
            if isinstance(seen, list):
                for key in seen[-self._MAX_SEEN:]:
                    if isinstance(key, str):
                        self._seen_ids[key] = None
            logger.info(
                "TelegramPoller resumed from %s (offset=%d, %d seen ids)",
                path, self._offset, len(self._seen_ids),
            )
        except Exception as exc:
            logger.warning(
                "TelegramPoller: ignoring unusable state file %s (%s) — "
                "continuing with an in-memory cursor", path, exc,
            )

    def _snapshot_state(self) -> dict[str, Any]:
        return {
            "token": self._state_key,
            "offset": self._offset,
            "seen_ids": list(self._seen_ids)[-self._MAX_SEEN:],
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
                    "TelegramPoller: cannot persist cursor to %s: %s", path, exc,
                )


    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._poll_timeout + 10))
        logger.info("TelegramPoller started (timeout=%ds)", self._poll_timeout)

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("TelegramPoller stopped")

    async def poll_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                updates = await self._fetch_updates()
                backoff = 1.0
                for update in updates:
                    await self._process_update(update)
            except asyncio.CancelledError:
                logger.info("Poller cancelled")
                break
            except _RateLimited as exc:
                delay = min(max(exc.retry_after, 1.0), 300.0)
                logger.warning("Poller rate limited by Telegram (retry in %.0fs)", delay)
                await asyncio.sleep(delay)
            except Exception as exc:
                logger.warning("Poller error (retry in %.0fs): %s", backoff, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


    async def _fetch_updates(self) -> list[dict[str, Any]]:
        if not self._client:
            return []

        params: dict[str, Any] = {
            "timeout": self._poll_timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if self._offset:
            params["offset"] = self._offset

        resp = await self._client.get(f"{self._base_url}/getUpdates", params=params)
        data = resp.json()

        if not data.get("ok"):
            if data.get("error_code") == 429:
                retry_after = (data.get("parameters") or {}).get("retry_after", 5)
                try:
                    retry_after = float(retry_after)
                except (TypeError, ValueError):
                    retry_after = 5.0
                raise _RateLimited(retry_after, data)
            raise RuntimeError(f"Telegram API error: {data}")

        results: list[dict] = data.get("result", [])
        if results:
            self._offset = results[-1]["update_id"] + 1
            await self._asave_state()
        return results

    async def _process_update(self, update: dict[str, Any]) -> None:
        callback_query = update.get("callback_query")
        if callback_query and self._on_callback_query:
            cb_chat_id = str(
                (callback_query.get("message") or {}).get("chat", {}).get("id", "")
            )
            if self._allowed_chat_ids is not None and cb_chat_id not in self._allowed_chat_ids:
                logger.warning(
                    "Telegram: rejecting callback from non-whitelisted chat %s", cb_chat_id,
                )
                return
            try:
                await self._on_callback_query(callback_query)
            except Exception as exc:
                logger.error("Error handling callback query: %s", exc, exc_info=True)
            return

        msg = update.get("message")
        if not msg:
            return

        text = msg.get("text", "")
        if not text:
            return

        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        message_id = msg.get("message_id")

        if self._allowed_chat_ids is not None and chat_id not in self._allowed_chat_ids:
            logger.warning(
                "Telegram: rejecting message from non-whitelisted chat %s "
                "(set TELEGRAM_ALLOWED_CHAT_IDS or chat_id in .env to allow)",
                chat_id,
            )
            return

        dedup_key = f"{chat_id}:{message_id}"
        if dedup_key in self._seen_ids:
            return
        self._seen_ids[dedup_key] = None
        if len(self._seen_ids) > self._MAX_SEEN:
            for _k in list(self._seen_ids)[: self._MAX_SEEN // 2]:
                del self._seen_ids[_k]
        await self._asave_state()

        sender = msg.get("from", {})
        sender_name = sender.get("first_name", "")
        if sender.get("last_name"):
            sender_name += f" {sender['last_name']}"

        ts = datetime.fromtimestamp(msg.get("date", 0), tz=timezone.utc)

        envelope = MessageEnvelope(
            message_id=dedup_key,
            channel=MessageChannel.TELEGRAM,
            sender_id=str(sender.get("id", "")),
            content=text,
            timestamp=ts,
            chat_id=chat_id,
            telegram_message_id=message_id,
            metadata={
                "sender_name": sender_name,
                "chat_type": chat.get("type", "private"),
                "chat_title": chat.get("title", ""),
            },
        )

        try:
            await self._on_message(envelope)
        except Exception as exc:
            logger.error("Error dispatching message: %s", exc, exc_info=True)
