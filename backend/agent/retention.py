from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)

RETENTION_SWEEP_INTERVAL_SECONDS: int = 24 * 60 * 60

_MEMORY_TABLES: tuple[str, ...] = ("reports", "message_evidence", "activity_log")


_MEDIA_SUBDIRS: tuple[str, ...] = ("uploads", "chat_images")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _cutoffs(retention_days: int) -> tuple[str, float]:
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=retention_days)
    iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")
    epoch = cutoff_dt.timestamp()
    return iso, epoch


def _prune_one_db(
    db_file: Path,
    deletes: list[tuple[str, str, Any]],
) -> dict[str, int]:
    results: dict[str, int] = {}
    if not db_file.exists():
        return results

    total_deleted = 0
    try:
        with sqlite3.connect(str(db_file), timeout=30, isolation_level=None) as conn:
            for table, column, cutoff in deletes:
                key = f"{db_file.name}:{table}"
                try:
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone()
                    if not exists:
                        results[key] = 0
                        continue
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE {column} < ?",
                        (cutoff,),
                    )
                    deleted = cur.rowcount if cur.rowcount is not None else 0
                    results[key] = max(deleted, 0)
                    total_deleted += results[key]
                except sqlite3.Error as e:
                    logger.warning(
                        "retention: DELETE failed for %s.%s: %s",
                        db_file.name, table, e,
                    )
                    results[key] = 0

            if total_deleted > 0:
                try:
                    conn.execute("VACUUM")
                except sqlite3.Error as e:
                    logger.warning(
                        "retention: VACUUM failed for %s: %s", db_file.name, e,
                    )
    except sqlite3.Error as e:
        logger.error("retention: could not open %s: %s", db_file, e)

    return results


async def _prune_health_stores(cutoff_iso: str) -> dict[str, int]:
    results: dict[str, int] = {}
    store_dir = Path(settings.DATA_STORE_PATH)
    if not store_dir.exists():
        return results
    for db_file in sorted(store_dir.glob("*_data.db")):
        try:
            part = await asyncio.to_thread(
                _prune_one_db,
                db_file,
                [("samples", "timestamp", cutoff_iso)],
            )
            results.update(part)
        except Exception as e:
            logger.error("retention: health store %s failed: %s", db_file, e)
    return results


async def _prune_watch_db(cutoff_epoch: float) -> dict[str, int]:
    watch_db = _repo_root() / "ios" / "Server" / "watch.db"
    if not watch_db.exists():
        return {}
    try:
        return await asyncio.to_thread(
            _prune_one_db,
            watch_db,
            [("health_samples_eav", "ts", cutoff_epoch)],
        )
    except Exception as e:
        logger.error("retention: watch.db prune failed: %s", e)
        return {}


async def _prune_memory_dbs(cutoff_iso: str) -> dict[str, int]:
    results: dict[str, int] = {}
    memory_dir = Path(settings.MEMORY_DB_PATH)
    if not memory_dir.exists():
        return results
    deletes = [(tbl, "created_at", cutoff_iso) for tbl in _MEMORY_TABLES]
    for db_file in sorted(memory_dir.glob("*.db")):
        try:
            part = await asyncio.to_thread(_prune_one_db, db_file, deletes)
            results.update(part)
        except Exception as e:
            logger.error("retention: memory DB %s failed: %s", db_file, e)
    return results


def _prune_media_files(store_dir: Path, cutoff_epoch: float) -> dict[str, int]:
    results: dict[str, int] = {}
    if not store_dir.is_dir():
        return results
    for user_dir in sorted(p for p in store_dir.iterdir() if p.is_dir()):
        for sub in _MEDIA_SUBDIRS:
            media_dir = user_dir / sub
            if not media_dir.is_dir():
                continue
            deleted = 0
            try:
                for f in media_dir.iterdir():
                    try:
                        if f.is_file() and f.stat().st_mtime < cutoff_epoch:
                            f.unlink()
                            deleted += 1
                    except OSError as e:
                        logger.warning("retention: could not remove %s: %s", f, e)
            except OSError as e:
                logger.warning("retention: cannot scan %s: %s", media_dir, e)
            results[f"{user_dir.name}/{sub}"] = deleted
    return results


async def prune_expired_data(retention_days: int) -> dict[str, int]:
    if retention_days is None or retention_days <= 0:
        logger.info("retention: disabled (retention_days=%s)", retention_days)
        return {}

    cutoff_iso, cutoff_epoch = _cutoffs(retention_days)
    logger.info(
        "retention: pruning rows older than %s (retention_days=%d)",
        cutoff_iso, retention_days,
    )

    results: dict[str, int] = {}

    try:
        results.update(await _prune_health_stores(cutoff_iso))
    except Exception as e:
        logger.error("retention: health-store sweep failed: %s", e, exc_info=True)

    try:
        results.update(await _prune_watch_db(cutoff_epoch))
    except Exception as e:
        logger.error("retention: watch.db sweep failed: %s", e, exc_info=True)

    try:
        results.update(await _prune_memory_dbs(cutoff_iso))
    except Exception as e:
        logger.error("retention: memory sweep failed: %s", e, exc_info=True)

    try:
        results.update(await asyncio.to_thread(
            _prune_media_files, Path(settings.DATA_STORE_PATH), cutoff_epoch,
        ))
    except Exception as e:
        logger.error("retention: media sweep failed: %s", e, exc_info=True)

    total = sum(results.values())
    logger.info(
        "retention: sweep complete — %d row(s) deleted across %d table(s)",
        total, len(results),
    )
    return results


async def retention_loop(app_state: Any | None = None) -> None:
    logger.info(
        "retention loop starting (interval=%ds, days=%d)",
        RETENTION_SWEEP_INTERVAL_SECONDS, settings.DATA_RETENTION_DAYS,
    )
    try:
        while True:
            try:
                await prune_expired_data(settings.DATA_RETENTION_DAYS)
            except Exception as e:
                logger.error("retention: sweep crashed: %s", e, exc_info=True)
            try:
                await asyncio.sleep(RETENTION_SWEEP_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
    except asyncio.CancelledError:
        logger.info("retention loop cancelled — exiting cleanly")
        raise
