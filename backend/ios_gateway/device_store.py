from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone

from ..config import settings

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_tokens (
    id              TEXT    PRIMARY KEY,
    user_id         TEXT    NOT NULL,
    device_token    TEXT    NOT NULL UNIQUE,
    bundle_id       TEXT,
    environment     TEXT    NOT NULL DEFAULT 'production',
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    last_seen_at    TEXT,
    revoked_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_devtok_user ON device_tokens(user_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _db_path() -> str:
    return str(settings.MEMORY_DB_PATH / "device_tokens.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def upsert_device_token(
    user_id: str,
    device_token: str,
    bundle_id: str | None = None,
    environment: str = "production",
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO device_tokens "
            "(id, user_id, device_token, bundle_id, environment, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(device_token) DO UPDATE SET "
            "  user_id=excluded.user_id, bundle_id=excluded.bundle_id, "
            "  environment=excluded.environment, last_seen_at=excluded.last_seen_at, "
            "  revoked_at=NULL",
            (str(uuid.uuid4()), user_id, device_token, bundle_id, environment, _now_iso()),
        )


def list_device_tokens(user_id: str, environment: str | None = None) -> list[dict]:
    sql = (
        "SELECT id, user_id, device_token, bundle_id, environment, created_at, last_seen_at "
        "FROM device_tokens WHERE user_id = ? AND revoked_at IS NULL"
    )
    params: list = [user_id]
    if environment:
        sql += " AND LOWER(environment) = ?"
        params.append(environment.lower())
    sql += " ORDER BY created_at DESC"
    with _connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def revoke_device_token(device_token: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE device_tokens SET revoked_at = ? "
            "WHERE device_token = ? AND revoked_at IS NULL",
            (_now_iso(), device_token),
        )
    return cur.rowcount > 0
