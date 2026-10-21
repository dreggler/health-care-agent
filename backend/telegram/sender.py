from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_MAX_MESSAGE_LENGTH = 4096
_TRUNCATION_SUFFIX = "\n\n<i>…message truncated</i>"


_CODE_BLOCK_RE = re.compile(r"```(\w*)\n?([\s\S]*?)```")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_DBL_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_SGL_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_ITALIC_RE = re.compile(r"(?<![_\w])_([^_]+)_(?![_\w])")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)[^>]*>")


_MAX_RETRY_AFTER_S = 30.0


def _retry_after_seconds(resp: httpx.Response) -> float:
    try:
        val = ((resp.json() or {}).get("parameters") or {}).get("retry_after")
        if val is not None:
            return min(max(float(val), 0.0), _MAX_RETRY_AFTER_S)
    except Exception:
        pass
    try:
        return min(max(float(resp.headers.get("Retry-After", 1)), 0.0), _MAX_RETRY_AFTER_S)
    except Exception:
        return 1.0


async def _post_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    resp = await client.post(url, **kwargs)
    if resp.status_code == 429:
        delay = _retry_after_seconds(resp)
        logger.warning("Telegram rate limited (429); retrying in %.1fs", delay)
        await asyncio.sleep(delay)
        resp = await client.post(url, **kwargs)
    return resp


def _html_to_plain(text: str) -> str:
    text = _TAG_RE.sub("", text)
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


def _cut_on_safe_boundary(chunk: str) -> str:
    lt = chunk.rfind("<")
    if lt > chunk.rfind(">"):
        chunk = chunk[:lt]
    amp = chunk.rfind("&")
    if amp > chunk.rfind(";") and len(chunk) - amp <= 8:
        chunk = chunk[:amp]
    return chunk


def _closing_tags(chunk: str) -> str:
    open_tags: list[str] = []
    for m in _TAG_RE.finditer(chunk):
        name = m.group(1).lower()
        if m.group(0).startswith("</"):
            for i in range(len(open_tags) - 1, -1, -1):
                if open_tags[i] == name:
                    del open_tags[i]
                    break
        else:
            open_tags.append(name)
    return "".join(f"</{name}>" for name in reversed(open_tags))


def _truncate_telegram_html(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    budget = max(0, limit - len(_TRUNCATION_SUFFIX))
    for _ in range(4):
        cut = _cut_on_safe_boundary(text[:budget])
        closers = _closing_tags(cut)
        if len(cut) + len(closers) <= budget:
            return cut + closers + _TRUNCATION_SUFFIX
        budget -= len(closers)
        if budget <= 0:
            return _TRUNCATION_SUFFIX.strip()
    return cut + closers + _TRUNCATION_SUFFIX


def _markdown_to_telegram_html(text: str) -> str:
    code_blocks: list[tuple[str, str]] = []

    def _save_cb(m: re.Match) -> str:
        code_blocks.append((m.group(1), m.group(2)))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(_save_cb, text)

    inline_codes: list[str] = []

    def _save_ic(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_save_ic, text)

    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")

    lines = text.split("\n")
    out: list[str] = []
    tables: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        hm = re.match(r"^#{1,6}\s+(.+)", stripped)
        if hm:
            out.append(f"\n<b>{hm.group(1)}</b>")
            i += 1
            continue

        if (
            stripped.startswith("|")
            and stripped.endswith("|")
            and stripped.count("|") >= 3
        ):
            table: list[str] = []
            while i < len(lines):
                s = lines[i].strip()
                if s.startswith("|") and s.endswith("|"):
                    table.append(lines[i])
                    i += 1
                else:
                    break
            if table:
                tables.append("\n".join(table))
                out.append(f"\x00TB{len(tables) - 1}\x00")
            continue

        lm = re.match(r"^(\s*)[-*+]\s+(.*)", lines[i])
        if lm:
            out.append(f"{lm.group(1)}\u2022 {lm.group(2)}")
            i += 1
            continue

        out.append(lines[i])
        i += 1

    text = "\n".join(out)

    text = _BOLD_DBL_RE.sub(r"<b>\1</b>", text)
    text = _BOLD_SGL_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)

    for idx, table_text in enumerate(tables):
        text = text.replace(f"\x00TB{idx}\x00", f"<pre>{table_text}</pre>")

    for idx, (_lang, code) in enumerate(code_blocks):
        esc = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{idx}\x00", f"<pre>{esc}</pre>")

    for idx, code in enumerate(inline_codes):
        esc = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{idx}\x00", f"<code>{esc}</code>")

    return text


class TelegramSender:

    def __init__(self, token: str, default_chat_id: str | None = None) -> None:
        self._token = token
        self._default_chat_id = default_chat_id
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client: httpx.AsyncClient | None = None
        self._muted_until: float | None = None


    def mute(self, duration_seconds: float) -> None:
        self._muted_until = time.time() + duration_seconds

    def unmute(self) -> None:
        self._muted_until = None

    def is_muted(self) -> bool:
        if self._muted_until is None:
            return False
        if time.time() >= self._muted_until:
            self._muted_until = None
            return False
        return True

    def mute_remaining(self) -> float | None:
        if not self.is_muted():
            return None
        return self._muted_until - time.time()


    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
            logger.info("TelegramSender started")

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("TelegramSender stopped")


    async def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        parse_mode: str = "HTML",
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> bool:
        target = chat_id or self._default_chat_id
        if not target:
            logger.warning("TelegramSender.send_message: no chat_id available")
            return False

        text = _markdown_to_telegram_html(text)

        text = _truncate_telegram_html(text, _MAX_MESSAGE_LENGTH)

        payload: dict = {
            "chat_id": target,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            resp = await _post_with_retry(client, f"{self._base_url}/sendMessage", json=payload)
            if resp.status_code == 200:
                logger.debug("Telegram message sent to %s", target)
                return True

            if resp.status_code == 400 and "parse" in resp.text.lower():
                payload["parse_mode"] = ""
                payload["text"] = _html_to_plain(payload["text"])
                resp2 = await _post_with_retry(client, f"{self._base_url}/sendMessage", json=payload)
                if resp2.status_code == 200:
                    logger.debug("Telegram message sent (plain) to %s", target)
                    return True
            logger.warning(
                "Telegram send failed (%d) to chat_id=%s: %s",
                resp.status_code, target, resp.text[:500],
            )
            return False
        except Exception as exc:
            logger.warning("Telegram send error: %s", exc)
            return False
        finally:
            if self._client is None:
                await client.aclose()

    async def send_photo(
        self,
        photo_path: str,
        caption: str = "",
        chat_id: str | None = None,
        parse_mode: str = "HTML",
        reply_markup: dict | None = None,
    ) -> bool:
        target = chat_id or self._default_chat_id
        if not target:
            logger.warning("TelegramSender.send_photo: no chat_id available")
            return False

        if caption:
            caption = _markdown_to_telegram_html(caption)
            caption = _truncate_telegram_html(caption, 1024)

        data: dict = {"chat_id": target, "caption": caption, "parse_mode": parse_mode}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)

        client = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            photo_bytes = await asyncio.to_thread(Path(photo_path).read_bytes)
            files = {"photo": ("chart.png", photo_bytes, "image/png")}
            resp = await _post_with_retry(
                client, f"{self._base_url}/sendPhoto", data=data, files=files,
            )
            if resp.status_code == 200:
                logger.debug("Telegram photo sent to %s", target)
                return True
            if resp.status_code == 400 and "parse" in resp.text.lower():
                data["caption"] = _html_to_plain(caption)
                data.pop("parse_mode", None)
                resp2 = await _post_with_retry(
                    client, f"{self._base_url}/sendPhoto", data=data, files=files,
                )
                if resp2.status_code == 200:
                    logger.debug("Telegram photo sent (plain caption) to %s", target)
                    return True
            logger.warning("Telegram photo failed (%d): %s", resp.status_code, resp.text[:300])
            return False
        except Exception as exc:
            logger.warning("Telegram photo error: %s", exc)
            return False
        finally:
            if self._client is None:
                await client.aclose()

    async def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict | None = None,
    ) -> bool:
        text = _truncate_telegram_html(
            _markdown_to_telegram_html(text), _MAX_MESSAGE_LENGTH,
        )

        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            resp = await _post_with_retry(client, f"{self._base_url}/editMessageText", json=payload)
            if resp.status_code == 200:
                return True

            if resp.status_code == 400 and "parse" in resp.text.lower():
                payload["parse_mode"] = ""
                payload["text"] = _html_to_plain(payload["text"])
                resp2 = await _post_with_retry(
                    client, f"{self._base_url}/editMessageText", json=payload
                )
                if resp2.status_code == 200:
                    return True
            logger.warning(
                "Telegram edit failed (%d): %s",
                resp.status_code,
                resp.text[:500],
            )
            return False
        except Exception as exc:
            logger.warning("Telegram edit error: %s", exc)
            return False
        finally:
            if self._client is None:
                await client.aclose()

    async def send_chat_action(
        self,
        chat_id: str,
        action: str = "typing",
    ) -> bool:
        client = self._client or httpx.AsyncClient(timeout=5.0)
        try:
            resp = await client.post(
                f"{self._base_url}/sendChatAction",
                json={"chat_id": chat_id, "action": action},
            )
            return resp.status_code == 200
        except Exception:
            return False
        finally:
            if self._client is None:
                await client.aclose()

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> bool:
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            payload = {
                "callback_query_id": callback_query_id,
                "text": text[:200] if text else "",
                "show_alert": show_alert,
            }
            resp = await client.post(f"{self._base_url}/answerCallbackQuery", json=payload)
            return resp.status_code == 200
        except Exception as exc:
            logger.debug("answerCallbackQuery error: %s", exc)
            return False
        finally:
            if self._client is None:
                await client.aclose()
