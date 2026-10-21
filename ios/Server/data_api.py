import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

_DEFAULT_DB = Path(__file__).parent / "watch.db"

_LEGACY_METRICS = ("hr", "hrv", "spo2", "cal", "steps", "rr")


def _get_available_metrics() -> tuple:
    try:
        con = _con()
        rows = con.execute(
            "SELECT DISTINCT f FROM health_samples_eav ORDER BY f"
        ).fetchall()
        con.close()
        if rows:
            return tuple(r[0] for r in rows)
    except Exception:
        pass
    return _LEGACY_METRICS


_metrics_cache: Optional[tuple] = None
_metrics_cache_ts: float = 0.0
_METRICS_TTL: float = 60.0


def _get_metrics() -> tuple:
    global _metrics_cache, _metrics_cache_ts
    import time
    now = time.time()
    if _metrics_cache is None or (now - _metrics_cache_ts) > _METRICS_TTL:
        _metrics_cache = _get_available_metrics()
        _metrics_cache_ts = now
    return _metrics_cache


def _con() -> sqlite3.Connection:
    path = os.environ.get("WATCH_DB", str(_DEFAULT_DB))
    return sqlite3.connect(path)


def _pivot_rows(rows: List[tuple]) -> List[Dict[str, Any]]:
    result = []
    current_ts = None
    current_row: Dict[str, Any] = {}

    for ts, f, v in rows:
        if current_ts is None or ts != current_ts:
            if current_row:
                result.append(current_row)
            current_ts = ts
            current_row = {"ts": ts}

        current_row[f] = v

    if current_row:
        result.append(current_row)

    return result


def get_latest() -> Dict[str, Any]:
    con = _con()
    result = {}
    for metric in _get_metrics():
        row = con.execute(
            "SELECT ts, v FROM health_samples_eav "
            "WHERE f = ? ORDER BY ts DESC LIMIT 1",
            (metric,)
        ).fetchone()
        if row:
            result[metric] = {"ts": row[0], "value": row[1]}
    con.close()
    return result


def get_recent(minutes: int = 10) -> List[Dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp()
    con = _con()
    try:
        rows = con.execute(
            "SELECT ts, f, v FROM health_samples_eav WHERE ts > ? ORDER BY ts ASC, f ASC",
            (since,)
        ).fetchall()
    finally:
        con.close()
    return _pivot_rows(rows)


def get_range(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    con = _con()
    try:
        rows = con.execute(
            "SELECT ts, f, v FROM health_samples_eav "
            "WHERE ts BETWEEN ? AND ? ORDER BY ts ASC, f ASC",
            (start.timestamp(), end.timestamp())
        ).fetchall()
    finally:
        con.close()
    return _pivot_rows(rows)


def get_summary(minutes: int = 60) -> Dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp()
    con = _con()

    count_row = con.execute(
        "SELECT COUNT(DISTINCT ts) FROM health_samples_eav WHERE ts > ?",
        (since,)
    ).fetchone()

    summary: Dict[str, Any] = {
        "period_minutes": minutes,
        "sample_count": count_row[0] if count_row else 0
    }

    for metric in _get_metrics():
        row = con.execute(
            "SELECT AVG(v), MIN(v), MAX(v) FROM health_samples_eav "
            "WHERE f = ? AND ts > ?",
            (metric, since)
        ).fetchone()

        if row and row[0] is not None:
            summary[metric] = {"avg": round(row[0], 2), "min": row[1], "max": row[2]}
        else:
            summary[metric] = {"avg": None, "min": None, "max": None}

    con.close()
    return summary


def to_llm_context(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)
