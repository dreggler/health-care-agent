import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .agent.retention import retention_loop
from .api.agent_routes import (
    get_active_agents_dict,
    get_memory_manager_for,
    shutdown_agents,
    start_system_ingestion,
    stop_all_ingestions,
    try_restore_agent,
)
from .api.agent_routes import (
    router as agent_router,
)
from .api.config_routes import (
    load_app_state,
    save_app_state_locked,
)
from .api.config_routes import (
    router as config_router,
)
from .api.data_routes import router as data_router
from .api.device_routes import router as device_router
from .api.page_routes import router as page_router
from .api.prompt_routes import router as prompt_router
from .api.skill_routes import router as skill_router
from .api.stream_routes import router as stream_router
from .config import settings
from .logging_config import setup_logging
from .services.streaming_service import shutdown_executor
from .utils import app_timezone, now_utc, parse_db_iso_utc, ts_fmt

logger = setup_logging()

_telegram_gateway = None
_feishu_gateway = None
_weixin_gateway = None
_ios_gateway = None

from .messaging.registry import GatewayRegistry

_gateway_registry: GatewayRegistry = GatewayRegistry()


def get_telegram_gateway():
    return _telegram_gateway


def get_feishu_gateway():
    return _feishu_gateway


def get_weixin_gateway():
    return _weixin_gateway


def get_ios_gateway():
    return _ios_gateway


def get_gateway_registry() -> GatewayRegistry:
    return _gateway_registry


def _claim_due_tasks(
    db_file: str, now: datetime, app_tz: ZoneInfo,
) -> list[tuple[int, str, datetime]]:
    import sqlite3 as _sqlite3

    import croniter as _croniter

    due: list[tuple[int, str, datetime, str | None]] = []
    with _sqlite3.connect(db_file, timeout=5, isolation_level=None) as conn:
        conn.row_factory = _sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, cron_expr, prompt_goal, last_run_at, created_at"
                " FROM scheduled_tasks WHERE status='active'"
            ).fetchall()

            for row in rows:
                anchor_utc = (
                    parse_db_iso_utc(row["last_run_at"])
                    or parse_db_iso_utc(row["created_at"])
                    or now
                )
                anchor_local = anchor_utc.astimezone(app_tz)
                try:
                    itr = _croniter.croniter(row["cron_expr"], anchor_local)
                    next_tick = itr.get_next(datetime)
                except Exception as e:
                    logger.warning("Scheduler: bad cron_expr for task %d: %s", row["id"], e)
                    continue

                if next_tick > now:
                    continue

                fired_tick = next_tick
                while True:
                    peek = itr.get_next(datetime)
                    if peek > now:
                        break
                    fired_tick = peek

                conn.execute(
                    "UPDATE scheduled_tasks SET last_run_at = ? WHERE id = ?",
                    (ts_fmt(fired_tick), row["id"]),
                )
                logger.info(
                    "Scheduler claim: pid=%d task=%d last_run_at %s -> %s UTC "
                    "(tick %s in %s; anchor=%s; now=%s UTC)",
                    os.getpid(), row["id"],
                    row["last_run_at"] or "NULL",
                    ts_fmt(fired_tick),
                    fired_tick.isoformat(),
                    app_tz.key,
                    anchor_local.isoformat(),
                    now.isoformat(),
                )
                due.append((row["id"], row["prompt_goal"], fired_tick, row["last_run_at"]))

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return due


def _release_task_claim(db_file: str, task_id: int, prev_last_run_at: str | None) -> None:
    import sqlite3 as _sqlite3

    with _sqlite3.connect(db_file, timeout=5) as conn:
        conn.execute(
            "UPDATE scheduled_tasks SET last_run_at = ? WHERE id = ?",
            (prev_last_run_at, task_id),
        )
        conn.commit()


async def _run_cron_scheduler():
    try:
        import croniter as _croniter
    except ImportError:
        logger.warning("croniter not installed — scheduled_tasks will not run. pip install croniter")
        return

    app_tz = app_timezone()
    logger.info("Cron scheduler started (timezone=%s)", app_tz.key)
    while True:
        try:
            now = now_utc().replace(microsecond=0)
            for pid, info in list(get_active_agents_dict().items()):
                if info.get("agent") is None:
                    continue
                memory = get_memory_manager_for(pid)
                if not memory:
                    continue
                try:
                    due = await asyncio.to_thread(
                        _claim_due_tasks, str(memory.db_file), now, app_tz,
                    )
                except Exception as e:
                    logger.warning("Cron scheduler: DB error for %s: %s", pid, e)
                    continue

                agent = info["agent"]
                for task_id, goal, fired_tick, prev_last_run in due:
                    logger.info(
                        "Scheduler: firing task %d for %s at tick %s (pid=%d, q=%d): %s",
                        task_id, pid, fired_tick.isoformat(),
                        os.getpid(), agent._analysis_queue.qsize(),
                        (goal or "")[:60],
                    )
                    try:
                        await agent.run_scheduled_analysis(goal, task_id=task_id)
                    except Exception as e:
                        logger.error("Scheduler: failed to enqueue task %d: %s", task_id, e)
                        try:
                            await asyncio.to_thread(
                                _release_task_claim,
                                str(memory.db_file), task_id, prev_last_run,
                            )
                        except Exception as rel_exc:
                            logger.error(
                                "Scheduler: failed to release claim on task %d: %s",
                                task_id, rel_exc,
                            )
        except Exception as e:
            logger.error("Scheduler loop error: %s", e)

        wall = now_utc()
        next_minute = wall.replace(second=0, microsecond=0) + timedelta(minutes=1)
        await asyncio.sleep(max(0.1, (next_minute - wall).total_seconds()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _telegram_gateway, _feishu_gateway, _weixin_gateway, _ios_gateway

    logger.info("[Startup] 1/4 Application starting up...")

    settings.validate_llm_keys()
    settings.validate_timezone()
    _warn_if_exposed_without_auth()

    logger.info("[Startup] 2/4 Creating directories (memory, logs)...")
    settings.MEMORY_DB_PATH.mkdir(parents=True, exist_ok=True)
    settings.AGENT_LOGS_PATH.mkdir(parents=True, exist_ok=True)
    (settings.MEMORY_DB_PATH / "agent_states").mkdir(parents=True, exist_ok=True)
    (settings.MEMORY_DB_PATH / "data_stores").mkdir(parents=True, exist_ok=True)

    await asyncio.to_thread(load_app_state)

    logger.info("[Startup] 3/4 Starting Telegram Gateway (if enabled)...")
    if settings.TELEGRAM_GATEWAY_ENABLED and settings.TELEGRAM_TOKEN:
        try:
            from .telegram import TelegramGateway

            allowed: set = set()
            if settings.CHAT_ID:
                allowed.add(settings.CHAT_ID)
            if settings.TELEGRAM_ALLOWED_CHAT_IDS:
                for cid in settings.TELEGRAM_ALLOWED_CHAT_IDS.split(","):
                    cid = cid.strip()
                    if cid:
                        allowed.add(cid)
            if not allowed:
                logger.warning(
                    "Telegram Gateway: no allowed chat IDs configured "
                    "(neither CHAT_ID nor TELEGRAM_ALLOWED_CHAT_IDS). "
                    "Inbound messages will be rejected. Set CHAT_ID in .env "
                    "to allow your own chat."
                )

            async def _on_user_message(envelope):
                agents = get_active_agents_dict()
                if not agents:
                    logger.info("on_user_message: no active agent — message ignored")
                    return
                pid, info = next(iter(agents.items()))
                agent = info["agent"]
                await agent.inbox.push(envelope)
                logger.info("on_user_message: routed to agent %s", pid)

            _telegram_gateway = TelegramGateway(
                token=settings.TELEGRAM_TOKEN,
                default_chat_id=settings.CHAT_ID,
                poll_timeout=settings.TELEGRAM_POLL_TIMEOUT,
                allowed_chat_ids=allowed,
                get_active_agents=get_active_agents_dict,
                get_memory_manager=get_memory_manager_for,
                on_user_message=_on_user_message,
            )
            await _telegram_gateway.start()
            _gateway_registry.register(_telegram_gateway)
            logger.info("Telegram Gateway started")
        except Exception as e:
            logger.warning(f"Telegram Gateway start failed: {e}", exc_info=True)
            _telegram_gateway = None
    elif settings.TELEGRAM_GATEWAY_ENABLED and not settings.TELEGRAM_TOKEN:
        logger.warning(
            "TELEGRAM_GATEWAY_ENABLED=true but TELEGRAM_TOKEN is not set — "
            "gateway will not start."
        )

    if settings.FEISHU_GATEWAY_ENABLED and settings.FEISHU_APP_ID and settings.FEISHU_APP_SECRET:
        try:
            from .feishu import FeishuGateway

            async def _on_feishu_user_message(envelope):
                agents = get_active_agents_dict()
                if not agents:
                    logger.info("Feishu on_user_message: no active agent — message ignored")
                    return
                pid, info = next(iter(agents.items()))
                agent = info["agent"]
                await agent.inbox.push(envelope)
                logger.info("Feishu on_user_message: routed to agent %s", pid)

            _feishu_gateway = FeishuGateway(
                settings=settings,
                on_user_message=_on_feishu_user_message,
                get_memory_manager=get_memory_manager_for,
            )
            try:
                _feishu_gateway.register_routes(app)
            except Exception as route_exc:
                logger.warning("Feishu webhook route registration skipped: %s", route_exc)
            await _feishu_gateway.start()
            _gateway_registry.register(_feishu_gateway)
            logger.info(
                "Feishu Gateway started (transport=%s)", settings.FEISHU_TRANSPORT,
            )
        except Exception as e:
            logger.warning(f"Feishu Gateway start failed: {e}", exc_info=True)
            _feishu_gateway = None
    elif settings.FEISHU_GATEWAY_ENABLED:
        logger.warning(
            "FEISHU_GATEWAY_ENABLED=true but FEISHU_APP_ID / FEISHU_APP_SECRET "
            "are not set — gateway will not start."
        )

    if settings.WEIXIN_GATEWAY_ENABLED:
        try:
            from .weixin import WeixinGateway

            async def _on_weixin_user_message(envelope):
                agents = get_active_agents_dict()
                if not agents:
                    logger.info(
                        "WeChat on_user_message: no active agent — message ignored"
                    )
                    return
                pid, info = next(iter(agents.items()))
                agent = info["agent"]
                await agent.inbox.push(envelope)
                logger.info("WeChat on_user_message: routed to agent %s", pid)

            _weixin_gateway = WeixinGateway(
                settings=settings,
                on_user_message=_on_weixin_user_message,
            )
            await _weixin_gateway.start()
            if _weixin_gateway.sender is not None:
                _gateway_registry.register(_weixin_gateway)
                logger.info("WeChat (Weixin) Gateway started")
            else:
                _weixin_gateway = None
        except Exception as e:
            logger.warning(f"WeChat Gateway start failed: {e}", exc_info=True)
            _weixin_gateway = None

    if settings.IOS_GATEWAY_ENABLED:
        try:
            from .ios_gateway import APNSSender, IOSGateway

            apns_sender = APNSSender(settings)
            _ios_gateway = IOSGateway(user_id="LiveUser", apns_sender=apns_sender)
            await _ios_gateway.start()
            _gateway_registry.register(_ios_gateway)
            logger.info("In-app iOS Gateway started")
            if apns_sender.enabled:
                logger.info(
                    "APNs push enabled (topic=%s, env=%s)",
                    settings.APNS_BUNDLE_ID, settings.APNS_ENV,
                )
        except Exception as e:
            logger.warning(f"iOS Gateway start failed: {e}", exc_info=True)
            _ios_gateway = None

    if settings.DATA_SOURCE == "live":
        logger.info("[Startup] Starting background data synchronization...")
        await start_system_ingestion("LiveUser")

    logger.info("[Startup] 4/4 Restoring agent (if AUTO_RESTORE_AGENT)...")
    try:
        await try_restore_agent()
    except Exception as e:
        logger.warning(f"Startup restore failed: {e}")

    logger.info("[Startup] Starting cron scheduler...")
    _scheduler_task = asyncio.create_task(_run_cron_scheduler(), name="cron_scheduler")
    _scheduler_task.add_done_callback(
        lambda t: logger.error("Cron scheduler crashed: %s", t.exception(), exc_info=t.exception())
        if not t.cancelled() and t.exception() else None
    )

    logger.info(
        "[Startup] Starting retention loop (retention=%d days)...",
        settings.DATA_RETENTION_DAYS,
    )
    _retention_task = asyncio.create_task(retention_loop(None), name="retention_loop")
    _retention_task.add_done_callback(
        lambda t: logger.error("Retention loop crashed: %s", t.exception(), exc_info=t.exception())
        if not t.cancelled() and t.exception() else None
    )

    logger.info("[Startup] All services ready — binding HTTP server on port %s", settings.API_PORT)
    yield

    logger.info("Application shutting down...")

    _scheduler_task.cancel()
    try:
        await _scheduler_task
    except asyncio.CancelledError:
        pass

    _retention_task.cancel()
    try:
        await _retention_task
    except asyncio.CancelledError:
        pass

    await stop_all_ingestions()

    await save_app_state_locked()

    if _telegram_gateway:
        try:
            await _telegram_gateway.stop()
        except Exception as e:
            logger.warning(f"Telegram Gateway stop error: {e}")
        _telegram_gateway = None

    if _feishu_gateway:
        try:
            await _feishu_gateway.stop()
        except Exception as e:
            logger.warning(f"Feishu Gateway stop error: {e}")
        _feishu_gateway = None

    if _weixin_gateway:
        try:
            await _weixin_gateway.stop()
        except Exception as e:
            logger.warning(f"WeChat Gateway stop error: {e}")
        _weixin_gateway = None

    if _ios_gateway:
        try:
            await _ios_gateway.stop()
        except Exception as e:
            logger.warning(f"iOS Gateway stop error: {e}")
        _ios_gateway = None

    _gateway_registry.clear()

    await shutdown_agents()

    shutdown_executor()

    logger.info("Shutdown complete.")

app = FastAPI(
    title="Wearable Data Analysis Platform",
    description="AI-powered analysis of wearable health data with LLM agents",
    version="1.0.0",
    lifespan=lifespan
)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}


def _warn_if_exposed_without_auth() -> None:
    host = (settings.API_HOST or "").strip().lower()
    if host in _LOOPBACK_HOSTS or settings.API_AUTH_TOKEN:
        return
    bar = "!" * 78
    logger.warning(
        "\n%s\nSECURITY WARNING: API_HOST=%s exposes the HIME API to the network and "
        "API_AUTH_TOKEN is empty,\nso anyone who can reach port %s can read your health "
        "data and drive the agent.\nSet API_AUTH_TOKEN in .env (and the same token in "
        "the iOS app / web UI), or bind API_HOST=127.0.0.1.\n%s",
        bar, settings.API_HOST, settings.API_PORT, bar,
    )


class BearerAuthMiddleware(BaseHTTPMiddleware):

    _QUERY_TOKEN_ANY_METHOD_PREFIXES = ("/api/personalised-pages/",)
    _QUERY_TOKEN_GET_PREFIXES = ("/api/agent/chat-image/",)

    @classmethod
    def _query_token_allowed(cls, method: str, path: str) -> bool:
        if path.startswith(cls._QUERY_TOKEN_ANY_METHOD_PREFIXES):
            return True
        return method in ("GET", "HEAD") and path.startswith(cls._QUERY_TOKEN_GET_PREFIXES)

    _PUBLIC_PATHS = {"/", "/health"}
    _DOCS_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}

    async def dispatch(self, request: Request, call_next):
        token = settings.API_AUTH_TOKEN
        if not token:
            return await call_next(request)

        path = request.url.path
        if request.method == "OPTIONS" or path in self._PUBLIC_PATHS:
            return await call_next(request)
        if path == getattr(settings, "FEISHU_WEBHOOK_PATH", "/api/feishu/webhook"):
            return await call_next(request)
        if not path.startswith("/api/") and path not in self._DOCS_PATHS:
            return await call_next(request)

        provided: str | None = None
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            provided = auth_header.split(" ", 1)[1].strip()
        if provided is None and self._query_token_allowed(request.method, path):
            provided = request.query_params.get("token")

        if not provided or not secrets.compare_digest(
            provided.encode("utf-8"), token.encode("utf-8"),
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


app.add_middleware(BearerAuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
)

app.include_router(config_router)
app.include_router(data_router)
app.include_router(agent_router)
app.include_router(page_router)
app.include_router(stream_router)
app.include_router(device_router)
app.include_router(prompt_router)
app.include_router(skill_router)

@app.get("/")
async def root():
    return {
        "name": "Wearable Data Analysis Platform",
        "version": "1.0.0",
        "status": "running"
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "data_source": "live",
    }


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": "An internal error occurred. See server logs for details."
        }
    )


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting server on {settings.API_HOST}:{settings.API_PORT}")

    import os
    enable_reload = os.environ.get("HIME_DEV_RELOAD", "").lower() in ("1", "true", "yes")
    uvicorn.run(
        "backend.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=enable_reload,
        reload_excludes=["data/**", "memory/**", "logs/**", "prompts/**", "*.db", "*.sqlite"],
        log_level="info"
    )
