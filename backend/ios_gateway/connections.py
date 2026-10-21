from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

_PRESENCE_TTL_S = 90.0


class _Conn:
    __slots__ = ("last_seen", "active")

    def __init__(self) -> None:
        self.last_seen = time.monotonic()
        self.active = False


class IOSConnectionRegistry:

    def __init__(self, ttl: float | None = None) -> None:
        self._conns: dict[str, dict[str, _Conn]] = {}
        self._lock = asyncio.Lock()
        self._ttl = ttl

    @property
    def ttl(self) -> float:
        return _PRESENCE_TTL_S if self._ttl is None else self._ttl

    async def register(self, user_id: str, conn_id: str) -> None:
        async with self._lock:
            self._conns.setdefault(user_id, {})[conn_id] = _Conn()
        logger.info(
            "iOS presence: +user=%s (conn=%s, online_users=%d)",
            user_id, conn_id, len(self._conns),
        )

    def touch(self, user_id: str, conn_id: str) -> None:
        conn = (self._conns.get(user_id) or {}).get(conn_id)
        if conn is not None:
            conn.last_seen = time.monotonic()
            conn.active = True

    async def unregister(self, user_id: str, conn_id: str) -> None:
        async with self._lock:
            conns = self._conns.get(user_id)
            removed = conns is not None and conns.pop(conn_id, None) is not None
            if conns is not None and not conns:
                self._conns.pop(user_id, None)
        if removed:
            logger.info("iOS presence: -user=%s (conn=%s)", user_id, conn_id)

    def is_online(self, user_id: str) -> bool:
        conns = self._conns.get(user_id)
        if not conns:
            return False
        now = time.monotonic()
        ttl = self.ttl
        return any(
            (not c.active) or (now - c.last_seen) < ttl
            for c in list(conns.values())
        )

    def online_users(self) -> list[str]:
        return [u for u in list(self._conns) if self.is_online(u)]


ios_connections = IOSConnectionRegistry()
