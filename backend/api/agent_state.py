from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict

from fastapi import HTTPException, Request
from pydantic import BaseModel, field_validator

from ..agent import MemoryManager
from ..config import settings

active_agents: dict[str, dict] = {}
system_ingest_tasks: dict[str, asyncio.Task] = {}
startup_lock = asyncio.Lock()

_RATE_LIMIT_WINDOW_S = 60
_RATE_LIMIT_MAX_CALLS = 5
_RATE_LIMIT_CHAT_MAX_CALLS = 60
_rate_buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
_RATE_BUCKETS_MAX_KEYS = 10_000


def _check_rate_limit(client_ip: str, endpoint: str = "default",
                      max_calls: int = _RATE_LIMIT_MAX_CALLS) -> None:
    now = time.monotonic()
    key = (client_ip, endpoint)
    bucket = [t for t in _rate_buckets[key] if now - t < _RATE_LIMIT_WINDOW_S]
    if len(bucket) >= max_calls:
        _rate_buckets[key] = bucket
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded: max {max_calls} calls "
                f"per {_RATE_LIMIT_WINDOW_S}s per IP for this endpoint."
            ),
        )
    bucket.append(now)
    _rate_buckets[key] = bucket
    _evict_stale_buckets(now)


def _evict_stale_buckets(now: float) -> None:
    if len(_rate_buckets) <= _RATE_BUCKETS_MAX_KEYS // 2:
        return
    for k in [k for k, v in _rate_buckets.items()
              if not v or now - v[-1] >= _RATE_LIMIT_WINDOW_S]:
        _rate_buckets.pop(k, None)
    if len(_rate_buckets) > _RATE_BUCKETS_MAX_KEYS:
        _rate_buckets.clear()


def _client_ip(request) -> str:
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                return parts[-1]
    return request.client.host if request.client else "unknown"


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_user_id(value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail="Invalid user id: use 1-64 letters, digits, '_' or '-'.",
        )
    return value


async def require_valid_ids(request: Request) -> None:
    for key in ("pid", "user_id"):
        val = request.path_params.get(key)
        if val is None:
            val = request.query_params.get(key)
        if val is not None:
            validate_user_id(val)


class StartAgentRequest(BaseModel):
    user_id:  str   = "LiveUser"
    llm_provider:    str   = "gemini"
    model:           str | None  = None
    granularity:     str   = "real-time"
    speed_multiplier: float = 1.0

    @field_validator("user_id")
    @classmethod
    def _check_user_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError("user_id must be 1-64 letters, digits, '_' or '-'")
        return v


class QuickAnalysisResponse(BaseModel):
    state: str
    message: str


class ChatMessageRequest(BaseModel):
    text: str = ""
    client_msg_id: str | None = None
    image_base64: str | None = None
    image_mime: str | None = None
    thread_id: str | None = None


class ChatStopRequest(BaseModel):
    user_id: str | None = None
    thread_id: str | None = None


def get_active_agents_dict() -> dict[str, dict]:
    return active_agents


def get_memory_manager_for(pid: str) -> MemoryManager | None:
    return _get_or_create_memory(pid)


def _get_or_create_memory(pid: str) -> MemoryManager | None:
    info = active_agents.get(pid)
    if info is not None and info.get("memory") is not None:
        return info["memory"]
    if not _ID_RE.match(pid or ""):
        return None
    db_file = settings.MEMORY_DB_PATH / f"{pid}.db"
    if not db_file.exists():
        return None
    return MemoryManager(settings.MEMORY_DB_PATH, pid)


async def aget_or_create_memory(pid: str) -> MemoryManager | None:
    info = active_agents.get(pid)
    if info is not None and info.get("memory") is not None:
        return info["memory"]
    return await asyncio.to_thread(_get_or_create_memory, pid)


def get_active_agent(user_id: str):
    info = active_agents.get(user_id)
    return info["agent"] if info else None
