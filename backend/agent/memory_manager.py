import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_schema_applied: set[str] = set()


def _db_file_key(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except Exception:
        return None
    path = row[2] if row and len(row) > 2 else None
    return path or None


def _ensure_schema(conn: sqlite3.Connection) -> None:
    key = _db_file_key(conn)
    if key is not None and key in _schema_applied:
        return
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reports (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            time_range_start TEXT,
            time_range_end   TEXT,
            title           TEXT,
            content         TEXT    NOT NULL,
            alert_level     TEXT    DEFAULT 'normal',
            metadata        TEXT,
            source          TEXT    DEFAULT 'scheduled_analysis'
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            event_type  TEXT,
            event_data  TEXT
        );

        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cron_expr TEXT NOT NULL,
            prompt_goal TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            last_run_at TEXT,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        );

        CREATE TABLE IF NOT EXISTS personalised_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            description TEXT,
            backend_route TEXT NOT NULL,
            frontend_asset TEXT NOT NULL,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS trigger_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            feature_type TEXT NOT NULL,
            condition TEXT NOT NULL,
            threshold REAL NOT NULL,
            window_minutes INTEGER DEFAULT 60,
            cooldown_minutes INTEGER DEFAULT 30,
            prompt_goal TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            last_triggered_at TEXT,
            trigger_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        );

        CREATE TABLE IF NOT EXISTS message_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_hash TEXT NOT NULL,
            message_text TEXT NOT NULL,
            tool_calls TEXT NOT NULL,
            verification_status TEXT DEFAULT 'verified',
            verification_detail TEXT,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        );

        CREATE TABLE IF NOT EXISTS chat_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            history_key   TEXT NOT NULL,
            role          TEXT NOT NULL,
            content       TEXT NOT NULL,
            message_hash  TEXT,
            client_msg_id TEXT,
            report_id     INTEGER,
            image_id      TEXT
        );

        CREATE TABLE IF NOT EXISTS chat_threads (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            created_at  TEXT,
            updated_at  TEXT,
            pinned      INTEGER DEFAULT 0,
            archived    INTEGER DEFAULT 0
        );

        INSERT OR IGNORE INTO chat_threads (id, title, created_at, updated_at, pinned, archived)
            VALUES ('main', 'CareAgent', strftime('%Y-%m-%dT%H:%M:%S', 'now'),
                    strftime('%Y-%m-%dT%H:%M:%S', 'now'), 1, 0);

        CREATE TABLE IF NOT EXISTS onboarding_survey (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            goals        TEXT,
            answers      TEXT,
            plan_status  TEXT DEFAULT 'pending'
        );

        CREATE INDEX IF NOT EXISTS idx_activity_log_id
            ON activity_log(id);
        CREATE INDEX IF NOT EXISTS idx_message_evidence_hash
            ON message_evidence(message_hash);
        CREATE INDEX IF NOT EXISTS idx_chat_history_key
            ON chat_history(history_key, id);
    """)
    conn.commit()

    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reports)").fetchall()}
        if "source" not in cols:
            conn.execute("ALTER TABLE reports ADD COLUMN source TEXT DEFAULT 'scheduled_analysis'")
            conn.commit()
    except Exception:
        pass

    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_history)").fetchall()}
        if "report_id" not in cols:
            conn.execute("ALTER TABLE chat_history ADD COLUMN report_id INTEGER")
            conn.commit()
    except Exception:
        pass

    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_history)").fetchall()}
        if "image_id" not in cols:
            conn.execute("ALTER TABLE chat_history ADD COLUMN image_id TEXT")
            conn.commit()
    except Exception:
        pass

    if key is not None:
        _schema_applied.add(key)


_chat_locks: dict[str, tuple[Any, asyncio.Lock]] = {}


def _get_chat_lock(db_file: Path) -> asyncio.Lock:
    key = str(db_file)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    entry = _chat_locks.get(key)
    if entry is None or entry[0] is not loop:
        lock = asyncio.Lock()
        _chat_locks[key] = (loop, lock)
        return lock
    return entry[1]


class MemoryManager:

    ACTIVITY_LIMIT = 2000

    def __init__(self, db_path: Path, user_id: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.mkdir(parents=True, exist_ok=True)
        self.user_id = user_id
        self.db_file = self.db_path / f"{user_id}.db"
        self._init_database()
        logger.debug("MemoryManager ready: %s", self.db_file)


    def _init_database(self) -> None:
        with sqlite3.connect(self.db_file, timeout=30) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            _ensure_schema(conn)
            for table in ("reports", "activity_log", "scheduled_tasks", "personalised_pages"):
                try:
                    conn.execute(
                        f"UPDATE [{table}] SET created_at = substr(created_at, 1, 19) "
                        f"WHERE created_at IS NOT NULL AND length(created_at) > 19"
                    )
                except Exception:
                    pass
            conn.commit()


    def get_recent_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_file, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM reports ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("metadata"):
                try:
                    d["metadata"] = json.loads(d["metadata"])
                except json.JSONDecodeError:
                    pass
            result.append(d)
        return result

    def delete_report(self, report_id: int) -> bool:
        with sqlite3.connect(self.db_file, timeout=30) as conn:
            cursor = conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
            conn.commit()
            return cursor.rowcount > 0


    def get_recent_activity(self, limit: int = 100) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_file, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM (
                       SELECT id, created_at, event_type, event_data
                       FROM activity_log
                       ORDER BY id DESC
                       LIMIT ?
                   ) ORDER BY id ASC""",
                (limit,),
            ).fetchall()
        events = []
        for row in rows:
            try:
                data = (
                    json.loads(row["event_data"], parse_constant=lambda _c: None)
                    if row["event_data"] else {}
                )
            except json.JSONDecodeError:
                data = {"raw": row["event_data"]}
            events.append(
                {
                    "created_at": row["created_at"],
                    "type": row["event_type"],
                    "data": data,
                }
            )
        return events

    async def persist_activity(self, event: dict[str, Any]) -> None:
        await asyncio.to_thread(self._persist_activity_sync, event)

    def _persist_activity_sync(self, event: dict[str, Any]) -> None:
        try:
            with sqlite3.connect(self.db_file, timeout=30) as conn:
                _ensure_schema(conn)
                conn.execute(
                    "INSERT INTO activity_log (event_type, event_data) VALUES (?, ?)",
                    (event.get("type", ""), json.dumps(event, default=str)),
                )
                count = conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
                if count > self.ACTIVITY_LIMIT:
                    excess = count - self.ACTIVITY_LIMIT
                    logger.info(
                        "Trimming %d old activity log entries (limit: %d)",
                        excess,
                        self.ACTIVITY_LIMIT,
                    )
                    conn.execute(
                        """DELETE FROM activity_log
                           WHERE id IN (
                               SELECT id FROM activity_log ORDER BY id LIMIT ?
                           )""",
                        (excess,),
                    )
                conn.commit()
        except Exception as exc:
            logger.warning("activity_log persist failed: %s", exc)


    CHAT_HISTORY_LIMIT = 2000

    def _chat_write_lock(self) -> asyncio.Lock:
        return _get_chat_lock(self.db_file)

    async def persist_chat_turn(
        self,
        history_key: str,
        role: str,
        content: str,
        message_hash: str | None = None,
        client_msg_id: str | None = None,
        report_id: int | None = None,
        image_id: str | None = None,
    ) -> None:
        await self.persist_chat_turns(history_key, [{
            "role": role, "content": content,
            "message_hash": message_hash, "client_msg_id": client_msg_id,
            "report_id": report_id, "image_id": image_id,
        }])

    async def persist_chat_turns(
        self,
        history_key: str,
        turns: list[dict[str, Any]],
    ) -> None:
        if not turns:
            return
        async with self._chat_write_lock():
            await asyncio.to_thread(self._persist_chat_turns_sync, history_key, turns)

    def _persist_chat_turns_sync(
        self,
        history_key: str,
        turns: list[dict[str, Any]],
    ) -> None:
        try:
            with sqlite3.connect(self.db_file, timeout=30) as conn:
                _ensure_schema(conn)
                conn.executemany(
                    "INSERT INTO chat_history "
                    "(history_key, role, content, message_hash, client_msg_id, report_id, image_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            history_key, t["role"], t["content"],
                            t.get("message_hash"), t.get("client_msg_id"),
                            t.get("report_id"), t.get("image_id"),
                        )
                        for t in turns
                    ],
                )
                from ..ios_gateway.threads import thread_id_from_history_key
                tid = thread_id_from_history_key(history_key)
                if tid is not None:
                    conn.execute(
                        "UPDATE chat_threads SET updated_at = "
                        "strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?",
                        (tid,),
                    )
                count = conn.execute(
                    "SELECT COUNT(*) FROM chat_history WHERE history_key = ?",
                    (history_key,),
                ).fetchone()[0]
                if count > self.CHAT_HISTORY_LIMIT:
                    excess = count - self.CHAT_HISTORY_LIMIT
                    conn.execute(
                        "DELETE FROM chat_history WHERE id IN ("
                        "  SELECT id FROM chat_history WHERE history_key = ? "
                        "  ORDER BY id LIMIT ?)",
                        (history_key, excess),
                    )
                conn.commit()
        except Exception as exc:
            logger.warning("chat_history persist failed: %s", exc)

    def get_chat_history(self, history_key: str, limit: int = 200) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_file, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM (
                       SELECT id, created_at, role, content, message_hash,
                              client_msg_id, report_id, image_id
                       FROM chat_history WHERE history_key = ?
                       ORDER BY id DESC LIMIT ?
                   ) ORDER BY id ASC""",
                (history_key, limit),
            ).fetchall()
        return [dict(r) for r in rows]


    def _thread_history_key(self, thread_id: str) -> str:
        from ..ios_gateway.threads import history_key_for
        return history_key_for(self.user_id, thread_id)

    def _thread_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["pinned"] = bool(d.get("pinned"))
        d["archived"] = bool(d.get("archived"))
        last = conn.execute(
            "SELECT role, content, created_at FROM chat_history "
            "WHERE history_key = ? ORDER BY id DESC LIMIT 1",
            (self._thread_history_key(d["id"]),),
        ).fetchone()
        d["last_message"] = (
            {"role": last[0], "content": (last[1] or "")[:120], "created_at": last[2]}
            if last else None
        )
        return d

    def list_chat_threads(self, include_archived: bool = False) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_file, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT * FROM chat_threads "
                + ("" if include_archived else "WHERE archived = 0 OR id = 'main' ")
                + "ORDER BY (id = 'main') DESC, pinned DESC, updated_at DESC, created_at DESC"
            ).fetchall()
            return [self._thread_row(conn, r) for r in rows]

    def get_chat_thread(self, thread_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_file, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM chat_threads WHERE id = ?", (thread_id,)
            ).fetchone()
            return self._thread_row(conn, row) if row else None

    def create_chat_thread(self, title: str = "") -> dict[str, Any]:
        import uuid
        tid = uuid.uuid4().hex
        with sqlite3.connect(self.db_file, timeout=30) as conn:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO chat_threads (id, title, created_at, updated_at, pinned, archived) "
                "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'), "
                "strftime('%Y-%m-%dT%H:%M:%S', 'now'), 0, 0)",
                (tid, (title or "").strip()[:80]),
            )
            conn.commit()
        return self.get_chat_thread(tid) or {"id": tid}

    def update_chat_thread(
        self,
        thread_id: str,
        title: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
        touch: bool = False,
    ) -> dict[str, Any] | None:
        sets: list[str] = []
        args: list[Any] = []
        if title is not None:
            sets.append("title = ?")
            args.append(title.strip()[:80])
        if pinned is not None:
            sets.append("pinned = ?")
            args.append(1 if pinned else 0)
        if archived is not None:
            sets.append("archived = ?")
            args.append(1 if archived else 0)
        if touch:
            sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
        if sets:
            with sqlite3.connect(self.db_file, timeout=30) as conn:
                _ensure_schema(conn)
                conn.execute(
                    f"UPDATE chat_threads SET {', '.join(sets)} WHERE id = ?",
                    (*args, thread_id),
                )
                conn.commit()
        return self.get_chat_thread(thread_id)

    def delete_chat_thread(self, thread_id: str) -> bool:
        if thread_id == "main":
            return False
        with sqlite3.connect(self.db_file, timeout=30) as conn:
            _ensure_schema(conn)
            conn.execute(
                "DELETE FROM chat_history WHERE history_key = ?",
                (self._thread_history_key(thread_id),),
            )
            cur = conn.execute("DELETE FROM chat_threads WHERE id = ?", (thread_id,))
            conn.commit()
            return cur.rowcount > 0

    def recent_user_text(self, limit: int = 12) -> str:
        try:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                rows = conn.execute(
                    "SELECT content FROM chat_history WHERE role = 'user' "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return "\n".join(str(r[0]) for r in rows if r and r[0])
        except Exception as exc:
            logger.debug("recent_user_text failed: %s", exc)
            return ""

    async def clear_chat_history(self, history_key: str) -> int:
        return await asyncio.to_thread(self._clear_chat_history_sync, history_key)

    def _clear_chat_history_sync(self, history_key: str) -> int:
        try:
            with sqlite3.connect(self.db_file, timeout=30) as conn:
                cur = conn.execute(
                    "DELETE FROM chat_history WHERE history_key = ?", (history_key,)
                )
                conn.commit()
                return cur.rowcount
        except Exception as exc:
            logger.warning("clear_chat_history failed: %s", exc)
            return 0


    def save_onboarding_survey(
        self, goals: list[str], answers: dict[str, Any] | None = None
    ) -> None:
        with sqlite3.connect(self.db_file, timeout=30) as conn:
            _ensure_schema(conn)
            conn.execute(
                "UPDATE onboarding_survey SET plan_status = 'superseded' "
                "WHERE plan_status = 'pending'"
            )
            conn.execute(
                "INSERT INTO onboarding_survey (goals, answers, plan_status) "
                "VALUES (?, ?, 'pending')",
                (json.dumps(goals or []), json.dumps(answers or {})),
            )
            conn.commit()

    def get_pending_survey(self) -> dict[str, Any] | None:
        try:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM onboarding_survey WHERE plan_status = 'pending' "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
        except Exception as exc:
            logger.debug("get_pending_survey failed: %s", exc)
            return None
        if not row:
            return None
        d = dict(row)
        for col in ("goals", "answers"):
            if d.get(col):
                try:
                    d[col] = json.loads(d[col])
                except json.JSONDecodeError:
                    pass
        return d

    def mark_survey_planned(self, survey_id: int) -> None:
        try:
            with sqlite3.connect(self.db_file, timeout=30) as conn:
                conn.execute(
                    "UPDATE onboarding_survey SET plan_status = 'done' WHERE id = ?",
                    (survey_id,),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("mark_survey_planned failed: %s", exc)


    def get_stats(self) -> dict[str, Any]:
        with sqlite3.connect(self.db_file, timeout=30) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            counts: dict[str, int] = {}
            for tbl in tables:
                try:
                    counts[tbl] = conn.execute(
                        f"SELECT COUNT(*) FROM [{tbl}]"
                    ).fetchone()[0]
                except Exception:
                    counts[tbl] = -1

            min_date = max_date = None
            for col, tbl in [("created_at", "reports"), ("created_at", "activity_log")]:
                if tbl in tables:
                    try:
                        row = conn.execute(
                            f"SELECT MIN({col}), MAX({col}) FROM [{tbl}]"
                        ).fetchone()
                        if row and (row[0] or row[1]):
                            min_date, max_date = row[0], row[1]
                            break
                    except Exception:
                        pass

        return {"table_counts": counts, "date_range": {"min": min_date, "max": max_date}}
