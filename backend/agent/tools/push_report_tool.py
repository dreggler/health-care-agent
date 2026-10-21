from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

import httpx

from ...agent.memory_manager import _ensure_schema
from ...config import settings
from ...messaging.registry import GatewayRegistry
from ...utils import ts_now
from .base import BaseTool
from .paths import IMAGE_EXTS, ChartPathError, resolve_chart_path

try:
    import sqlite3
except ImportError:
    sqlite3 = None

logger = logging.getLogger(__name__)

_IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)\s*\)")
_IMG_EXTS = IMAGE_EXTS
_MAX_IMG_BYTES = 2_000_000
_MAX_EMBEDDED = 8


def _file_to_data_uri(src: str) -> str | None:
    try:
        path = resolve_chart_path(src, max_bytes=_MAX_IMG_BYTES)
    except ChartPathError as exc:
        logger.warning("push_report: skipping chart %s: %s", src, exc)
        return None
    try:
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as exc:
        logger.warning("push_report: failed to embed chart %s: %s", src, exc)
        return None


def _embed_local_charts(content: str, extra_paths: list[str] | None) -> str:
    embedded = 0

    def _repl(m: re.Match) -> str:
        nonlocal embedded
        alt, src = m.group(1), m.group(2)
        if src.startswith(("data:", "http://", "https://")) or embedded >= _MAX_EMBEDDED:
            return m.group(0)
        uri = _file_to_data_uri(src)
        if uri is None:
            return m.group(0)
        embedded += 1
        return f"![{alt}]({uri})"

    out = _IMG_RE.sub(_repl, content or "")

    for p in (extra_paths or []):
        if embedded >= _MAX_EMBEDDED:
            break
        if Path(p).name in out:
            continue
        uri = _file_to_data_uri(p)
        if uri is None:
            continue
        embedded += 1
        out += f"\n\n![chart]({uri})"
    return out


class PushReportTool(BaseTool):

    name = "push_report"

    _VALID_ALERT_LEVELS = ("normal", "info", "warning", "critical")
    _VALID_SOURCES = ("scheduled_analysis", "quick_analysis")

    def __init__(
        self,
        memory_db_path: Path,
        user_id: str,
        report_callback=None,
        telegram_sender=None,
        fact_verifier=None,
        gateway_registry: GatewayRegistry | None = None,
    ) -> None:
        self.memory_db_file  = memory_db_path / f"{user_id}.db"
        self.user_id  = user_id
        self.report_callback = report_callback
        self._telegram_sender = telegram_sender
        self._gateway_registry = gateway_registry
        self._fact_verifier = fact_verifier
        self._llm_provider = None
        self._memory = None
        self._current_tool_results: list[dict[str, Any]] = []


    def get_definition(self) -> dict:
        return self._get_definition_from_json("push_report")


    async def execute(
        self,
        title: str = "",
        content: str = "",
        im_digest: str = "",
        time_range_start: str = "",
        time_range_end: str = "",
        alert_level: str = "normal",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        source: str = "scheduled_analysis",
        image_paths: list[str] | None = None,
    ) -> dict:
        if not title or not content:
            return {
                "success": False,
                "error": "push_report requires at least 'title' and 'content'. "
                         "Also provide: im_digest, time_range_start, time_range_end.",
            }

        content = await asyncio.to_thread(_embed_local_charts, content, image_paths)
        if not time_range_start:
            time_range_start = ts_now()
        if not time_range_end:
            time_range_end = ts_now()
        if not im_digest:
            im_digest = title

        if alert_level not in self._VALID_ALERT_LEVELS:
            return {
                "success": False,
                "error": f"Invalid alert_level. Must be one of: {self._VALID_ALERT_LEVELS}",
            }

        if source not in self._VALID_SOURCES:
            source = "scheduled_analysis"

        report_meta: dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}
        if tags:
            report_meta["tags"] = tags
        report_meta["user_id"] = self.user_id
        report_meta["im_digest"] = im_digest
        report_meta["source"] = source

        _EMOJI = {"normal": "\u2705", "info": "\u2139\ufe0f", "warning": "\u26a0\ufe0f", "critical": "\U0001f6a8"}
        im_body = im_digest or content
        emoji = _EMOJI.get(alert_level, "\U0001f4dd")
        im_text = f"{emoji} **{title}**\n\n{im_body}"

        verification = await self._verify_and_build_markup(im_text)
        if verification["status"] in ("fabricated", "unverified"):
            logger.warning(
                "push_report blocked (%s): %s", verification["status"], verification["detail"],
            )
            return {
                "success": False,
                "error": (
                    f"Report blocked by fact verification ({verification['status']}): "
                    f"{verification['detail']}. "
                    "Use sql/code tools to query real data before pushing a report."
                ),
            }

        t0 = time.perf_counter()
        report_id = await asyncio.to_thread(
            self._save_to_db,
            title, content, time_range_start, time_range_end, alert_level, report_meta, source,
        )
        elapsed = time.perf_counter() - t0
        logger.info("push_report: _save_to_db took %.2fs", elapsed)

        if report_id < 0:
            logger.error("Database write failed for report '%s'.", title)
            return {
                "success": False,
                "error": "DB insert failed — report was not saved. Check logs for details.",
            }

        if report_id == 0:
            logger.error("Database insert returned no rowid for report '%s'.", title)
            return {
                "success": False,
                "error": "DB insert returned no rowid — report may not have been saved.",
            }

        report: dict[str, Any] = {
            "id":               report_id,
            "created_at":       ts_now(),
            "time_range_start": time_range_start,
            "time_range_end":   time_range_end,
            "title":            title,
            "content":          content,
            "alert_level":      alert_level,
            "metadata":         report_meta,
            "user_id":   self.user_id,
            "im_digest":        im_digest,
            "source":           source,
        }

        if self.report_callback:
            try:
                self.report_callback(report)
            except Exception as exc:
                logger.warning("report_callback failed: %s", exc)

        report["_im_text"] = im_text
        report["_reply_markup"] = verification["reply_markup"]

        from ..agent_loops import spawn_background
        spawn_background(self._notify_user(report), label="User notify")

        logger.info("Report saved: '%s' (id=%d, level=%s, source=%s)", title, report_id, alert_level, source)
        return {
            "success":    True,
            "report_id":  report_id,
            "title":      title,
            "alert_level": alert_level,
            "source":     source,
            "message":    "Report saved and pushed successfully.",
        }


    def _save_to_db(
        self,
        title: str,
        content: str,
        time_range_start: str,
        time_range_end: str,
        alert_level: str,
        metadata: dict,
        source: str = "scheduled_analysis",
    ) -> int:
        try:
            import sqlite3 as _sqlite3
            with _sqlite3.connect(self.memory_db_file, timeout=30) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                _ensure_schema(conn)
                cur = conn.execute(
                    """INSERT INTO reports
                       (time_range_start, time_range_end, title, content, alert_level, metadata, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        time_range_start, time_range_end,
                        title, content, alert_level,
                        json.dumps(metadata),
                        source,
                    ),
                )
                conn.commit()
                return cur.lastrowid or -1
        except Exception as exc:
            logger.error("Failed to save report to DB: %s", exc, exc_info=True)
            return -1


    async def _persist_ios_history(
        self, message: str, reply_markup: dict | None, report_id: int | None = None
    ) -> None:
        try:
            from ...ios_gateway.gateway import extract_message_hash
            from ..memory_manager import MemoryManager
            msg_hash = extract_message_hash(reply_markup)
            mem = self._memory
            if mem is None:
                mem = await asyncio.to_thread(
                    MemoryManager, self.memory_db_file.parent, self.user_id,
                )
                self._memory = mem
            await mem.persist_chat_turn(
                f"ios:{self.user_id}", "assistant", message,
                message_hash=msg_hash, report_id=report_id,
            )
        except Exception as exc:
            logger.debug("iOS chat-history persist for proactive report skipped: %s", exc)

    async def _notify_user(self, report: dict[str, Any]) -> None:
        message = report.get("_im_text", "")
        if not message:
            _EMOJI = {"normal": "\u2705", "info": "\u2139\ufe0f", "warning": "\u26a0\ufe0f", "critical": "\U0001f6a8"}
            emoji = _EMOJI.get(report.get("alert_level", ""), "\U0001f4dd")
            body = report.get("im_digest") or report.get("content", "")
            message = f"{emoji} **{report['title']}**\n\n{body}"

        reply_markup = report.get("_reply_markup")
        report_id = report.get("id")

        if self._gateway_registry is not None and len(self._gateway_registry) > 0:
            has_ios = False
            for gw in self._gateway_registry.all():
                is_ios = getattr(getattr(gw, "channel", None), "value", None) == "ios"
                if is_ios:
                    has_ios = True
                target = getattr(gw, "default_chat_id", None)
                if not target:
                    continue
                try:
                    if gw.is_muted():
                        logger.info(
                            "%s gateway muted — skipping notification for '%s'",
                            gw.channel.value, report.get("title", "?"),
                        )
                        continue
                    send_kwargs = {"text": message, "chat_id": target, "reply_markup": reply_markup}
                    if is_ios:
                        send_kwargs["report_id"] = report_id
                    ok = await gw.send_message(**send_kwargs)
                    if ok:
                        logger.info(
                            "Notification sent via %s for report '%s'",
                            gw.channel.value, report["title"],
                        )
                    else:
                        logger.warning(
                            "Notification failed via %s for report '%s'",
                            gw.channel.value, report["title"],
                        )
                except Exception as exc:
                    logger.warning("%s notification error: %s", gw.channel.value, exc)
            if has_ios:
                await self._persist_ios_history(message, reply_markup, report_id=report_id)
            return

        if self._telegram_sender is not None:
            if getattr(self._telegram_sender, "is_muted", lambda: False)():
                logger.info("Telegram muted — skipping notification for '%s'", report.get("title", "?"))
                return
            try:
                ok = await self._telegram_sender.send_message(
                    text=message, reply_markup=reply_markup,
                )
                if ok:
                    logger.info("Telegram notification sent for report '%s'", report["title"])
                else:
                    logger.warning("Telegram notification failed for report '%s'", report["title"])
            except Exception as exc:
                logger.warning("Telegram notification error: %s", exc)
            return

        if not getattr(settings, "TELEGRAM_GATEWAY_ENABLED", False):
            return
        token   = getattr(settings, "TELEGRAM_TOKEN", None)
        chat_id = getattr(settings, "CHAT_ID", None)
        if not token or not chat_id:
            return

        from ...telegram.sender import _markdown_to_telegram_html
        url     = f"https://api.telegram.org/bot{token}/sendMessage"
        payload: dict = {"chat_id": chat_id, "text": _markdown_to_telegram_html(message), "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    logger.info("Telegram notification sent for report '%s'", report["title"])
                elif resp.status_code == 400 and "parse" in resp.text.lower():
                    payload["parse_mode"] = ""
                    resp2 = await client.post(url, json=payload)
                    if resp2.status_code == 200:
                        logger.info("Telegram notification sent (plain) for report '%s'", report["title"])
                    else:
                        logger.warning("Telegram notification failed (plain): %s", resp2.text[:200])
                else:
                    logger.warning("Telegram notification failed (%d): %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Telegram notification error: %s", exc)


_VALID_ALERT_LEVELS = PushReportTool._VALID_ALERT_LEVELS
