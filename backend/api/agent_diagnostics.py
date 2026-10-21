from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import sqlite3

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import settings
from .agent_state import active_agents, aget_or_create_memory

logger = logging.getLogger(__name__)

diagnostics_router = APIRouter()

_LIVE_USER = "LiveUser"


@diagnostics_router.get("/evidence/{message_hash}")
async def get_message_evidence(message_hash: str):
    def _load():
        from ..agent.fact_verifier import FactVerifier
        fv = FactVerifier(settings.MEMORY_DB_PATH, _LIVE_USER)
        ev = fv.get_evidence(message_hash)
        if not ev:
            return None, ""
        return ev, fv.format_evidence_for_display(ev)

    evidence, formatted = await asyncio.to_thread(_load)
    if not evidence:
        return {"success": True, "found": False, "evidence": None, "formatted": ""}
    return {"success": True, "found": True, "evidence": evidence, "formatted": formatted}


@diagnostics_router.get("/chat-history")
async def get_chat_history(
    limit: int = Query(200, ge=1, le=2000),
    thread_id: str = Query("main"),
):
    from ..ios_gateway.threads import history_key_for
    from .agent_threads import check_thread_id
    tid = check_thread_id(thread_id)
    memory = await aget_or_create_memory(_LIVE_USER)
    if memory is None:
        return {"success": True, "messages": []}
    if tid != "main" and await asyncio.to_thread(memory.get_chat_thread, tid) is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages = await asyncio.to_thread(
        memory.get_chat_history, history_key_for(_LIVE_USER, tid), limit,
    )
    return {"success": True, "messages": messages}


class OnboardingSurvey(BaseModel):
    goals: list[str] = []
    answers: dict = {}
    trigger_now: bool = False


@diagnostics_router.post("/onboarding-survey")
async def post_onboarding_survey(body: OnboardingSurvey):
    memory = await aget_or_create_memory(_LIVE_USER)
    if memory is None:
        from ..agent import MemoryManager
        memory = await asyncio.to_thread(MemoryManager, settings.MEMORY_DB_PATH, _LIVE_USER)
    await asyncio.to_thread(memory.save_onboarding_survey, body.goals, body.answers)
    logger.info("Onboarding survey saved (%d goals)", len(body.goals))

    triggered = False
    if body.trigger_now:
        info = active_agents.get(_LIVE_USER)
        agent = info.get("agent") if info else None
        if agent is not None and hasattr(agent, "trigger_plan_redesign"):
            triggered = await agent.trigger_plan_redesign()
        if not triggered:
            logger.info(
                "Redesign requested but agent not ready — will run on next chat reply"
            )
    queued = triggered or not body.trigger_now
    return {
        "success": True,
        "queued_plan": queued,
        "triggered_now": triggered,
        "deferred": (not triggered),
    }


@diagnostics_router.get("/chat-image/{image_id}")
async def get_chat_image(image_id: str):
    from ..ios_gateway.image_store import image_store

    path = image_store.get(image_id, _LIVE_USER)
    if not path or not os.path.exists(path):
        path = image_store.durable_path(image_id, _LIVE_USER)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Image not found")
    media_type = mimetypes.guess_type(path)[0] or "image/png"
    return FileResponse(path, media_type=media_type)


@diagnostics_router.get("/chat-info")
async def get_chat_info():
    if getattr(settings, "FEISHU_GATEWAY_ENABLED", False):
        return {
            "platform": "feishu",
            "label":    "Chat on Feishu",
            "url":      "lark://",
        }

    if getattr(settings, "TELEGRAM_GATEWAY_ENABLED", False):
        return {
            "platform": "telegram",
            "label":    "Chat on Telegram",
            "url":      "tg://",
        }

    return {"platform": "none", "label": "Chat", "url": ""}


@diagnostics_router.get("/telegram-info")
async def get_telegram_info():
    bot_username = ""
    chat_id = ""
    try:
        if settings.TELEGRAM_TOKEN:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"https://api.telegram.org/bot{settings.TELEGRAM_TOKEN}/getMe", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    bot_username = data["result"].get("username", "")
        chat_ids = settings.TELEGRAM_ALLOWED_CHAT_IDS
        if chat_ids:
            chat_id = chat_ids.split(",")[0].strip()
        elif settings.CHAT_ID:
            chat_id = settings.CHAT_ID.strip()
    except Exception as e:
        logger.debug("telegram-info error: %s", e)
    return {
        "bot_username": bot_username,
        "chat_id": chat_id,
        "group_link": settings.TELEGRAM_GROUP_LINK or "",
    }


@diagnostics_router.get("/activity/{pid}")
async def get_agent_activity(pid: str, limit: int = Query(500, ge=1, le=2000)):
    try:
        memory = await aget_or_create_memory(pid)
        if not memory:
            return {"success": True, "user_id": pid, "events": []}
        events = await asyncio.to_thread(memory.get_recent_activity, limit)
        return {"success": True, "user_id": pid, "events": events}
    except Exception as exc:
        logger.exception("Error fetching activity for %s: %s", pid, exc)
        return {
            "success": False,
            "user_id": pid,
            "events": [],
            "error": "Failed to read activity log.",
        }


@diagnostics_router.get("/memory/{pid}")
async def query_agent_memory(pid: str, query_type: str = "stats"):
    try:
        memory = await aget_or_create_memory(pid)
        if not memory:
            return {"success": True, "user_id": pid, "query_type": query_type, "data": [] if query_type == "reports" else {}}
        if query_type == "stats":
            data = await asyncio.to_thread(memory.get_stats)
        elif query_type == "reports":
            data = await asyncio.to_thread(memory.get_recent_reports, 20)
        else:
            raise ValueError(f"Invalid query_type '{query_type}'. Use 'stats' or 'reports'.")
        return {"success": True, "user_id": pid, "query_type": query_type, "data": data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Error querying memory for %s: %s", pid, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to query memory.") from exc


@diagnostics_router.delete("/memory/{pid}/reports/{report_id}")
async def delete_agent_report(pid: str, report_id: int):
    try:
        memory = await aget_or_create_memory(pid)
        if not memory:
            raise HTTPException(status_code=404, detail=f"No memory found for user {pid}")
        removed = await asyncio.to_thread(memory.delete_report, report_id)
        if not removed:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")
        return {"success": True, "user_id": pid, "deleted_id": report_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error deleting report %s for %s: %s", report_id, pid, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete report.") from exc


@diagnostics_router.get("/tools")
async def list_tools(user_id: str | None = None):
    live_agent = (active_agents.get(user_id) or {}).get("agent") if user_id else None
    if live_agent is not None:
        tools = live_agent.tool_registry.get_definitions()
    else:
        def _default_definitions() -> list:
            from ..agent.autonomous_agent import AutonomousHealthAgent
            from ..agent.skills.registry import SkillRegistry
            from ..agent.tools.registry import ToolRegistry
            skill_registry = SkillRegistry(roots=AutonomousHealthAgent._resolve_skill_roots())
            return ToolRegistry.with_default_tools(
                data_store=None,
                memory_db_path=settings.MEMORY_DB_PATH,
                user_id="dummy",
                skill_registry=skill_registry,
            ).get_definitions()

        tools = await asyncio.to_thread(_default_definitions)

    return {"success": True, "tools": tools}


@diagnostics_router.get("/memory/{pid}/inspect")
async def inspect_memory_table(
    pid: str,
    table_name: str = Query(...),
    limit: int = Query(50, ge=1, le=200)
):
    try:
        memory = await aget_or_create_memory(pid)
        if not memory:
            raise HTTPException(status_code=404, detail=f"No memory found for user {pid}")

        def _read_rows() -> list[dict]:
            with sqlite3.connect(memory.db_file) as conn:
                conn.row_factory = sqlite3.Row
                existing = {
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                if table_name not in existing:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Table '{table_name}' not found. Available: {sorted(existing)}",
                    )
                cursor = conn.execute(
                    f"SELECT * FROM [{table_name}] ORDER BY ROWID DESC LIMIT ?", (limit,)
                )
                return [dict(r) for r in cursor.fetchall()]

        rows = await asyncio.to_thread(_read_rows)

        return {
            "success": True,
            "user_id": pid,
            "table_name": table_name,
            "rows": rows
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error inspecting memory table %s for %s: %s", table_name, pid, exc)
        raise HTTPException(
            status_code=500, detail="Failed to inspect memory table."
        ) from exc
