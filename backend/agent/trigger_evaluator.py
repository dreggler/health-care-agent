from __future__ import annotations

import asyncio
import logging
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..utils import ts_fmt, ts_now

logger = logging.getLogger(__name__)

_SIMPLE_CONDITIONS = {"gt", "lt", "gte", "lte"}
_WINDOW_CONDITIONS = {"avg_gt", "avg_lt", "spike", "drop", "delta_gt", "absent"}
_ALL_CONDITIONS = _SIMPLE_CONDITIONS | _WINDOW_CONDITIONS


def _is_stale(timestamp: Any, window_minutes: Any) -> bool:
    try:
        window = float(window_minutes) if window_minutes else 60.0
        text = str(timestamp).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) > timedelta(minutes=window)
    except (ValueError, TypeError):
        return False


class TriggerEvaluator:

    def __init__(
        self,
        memory_db_path: Path,
        user_id: str,
        health_db_path: Path,
    ) -> None:
        self.memory_db_file = memory_db_path / f"{user_id}.db"
        self.health_db_file = health_db_path
        self.user_id = user_id
        self._last_eval_time: float = float("-inf")
        self._min_eval_interval: float = 10.0
        self._pending_features: set[str] = set()
        self._pending_all: bool = False

    async def evaluate_after_ingest(
        self,
        agent_queue: asyncio.Queue | None = None,
        ingested_features: set | None = None,
    ) -> list[dict[str, Any]]:
        now = time.monotonic()
        if now - self._last_eval_time < self._min_eval_interval:
            if ingested_features:
                self._pending_features |= set(ingested_features)
            else:
                self._pending_all = True
            return []
        self._last_eval_time = now

        features: set | None
        if ingested_features and not self._pending_all:
            features = set(ingested_features) | self._pending_features
        else:
            features = None
        self._pending_features = set()
        self._pending_all = False

        rules = await asyncio.to_thread(self._load_active_rules)
        if not rules:
            return []

        rules = [r for r in rules if r["condition"] != "absent"]
        if features:
            rules = [r for r in rules if r["feature_type"] in features]

        return await self._fire_rules(rules, agent_queue)

    async def evaluate_periodic(
        self, agent_queue: asyncio.Queue | None = None,
    ) -> list[dict[str, Any]]:
        pending = set(self._pending_features)
        pending_all = self._pending_all
        self._pending_features = set()
        self._pending_all = False

        rules = await asyncio.to_thread(self._load_active_rules)
        if not rules:
            return []
        selected = [
            r for r in rules
            if r["condition"] == "absent"
            or pending_all
            or r["feature_type"] in pending
        ]
        return await self._fire_rules(selected, agent_queue)

    async def _fire_rules(
        self, rules: list[dict[str, Any]], agent_queue: asyncio.Queue | None,
    ) -> list[dict[str, Any]]:
        triggered: list[dict[str, Any]] = []
        for rule in rules:
            if self._is_on_cooldown(rule):
                continue
            try:
                fired = await asyncio.to_thread(self._evaluate_rule, rule)
            except Exception as exc:
                logger.warning("Trigger rule %s eval error: %s", rule.get("id"), exc)
                continue

            if fired:
                triggered.append(rule)
                await asyncio.to_thread(self._mark_triggered, rule["id"])
                goal = (
                    f"[Triggered: {rule['name']}] "
                    f"{rule['prompt_goal']}"
                )
                logger.info(
                    "Trigger fired: rule=%d name='%s' feature=%s condition=%s",
                    rule["id"], rule["name"], rule["feature_type"], rule["condition"],
                )
                if agent_queue is not None:
                    try:
                        agent_queue.put_nowait(goal)
                    except asyncio.QueueFull:
                        logger.warning(
                            "Analysis queue full — dropping trigger goal for rule %d ('%s')",
                            rule["id"], rule["name"],
                        )

        return triggered


    def _load_active_rules(self) -> list[dict[str, Any]]:
        try:
            with sqlite3.connect(str(self.memory_db_file), timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM trigger_rules WHERE status = 'active'"
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Failed to load trigger rules: %s", exc)
            return []


    @staticmethod
    def _is_on_cooldown(rule: dict[str, Any]) -> bool:
        last_triggered = rule.get("last_triggered_at")
        if not last_triggered:
            return False
        cooldown_min = rule.get("cooldown_minutes", 30)
        try:
            last_dt = datetime.strptime(last_triggered[:19], "%Y-%m-%dT%H:%M:%S")
            last_dt = last_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return (now - last_dt) < timedelta(minutes=cooldown_min)
        except (ValueError, TypeError):
            return False


    def _evaluate_rule(self, rule: dict[str, Any]) -> bool:
        condition = rule["condition"]
        feature = rule["feature_type"]
        threshold = rule["threshold"]
        window_min = rule.get("window_minutes", 60)

        if condition not in _ALL_CONDITIONS:
            logger.warning("Unknown trigger condition: %s", condition)
            return False

        with sqlite3.connect(str(self.health_db_file), timeout=10) as conn:
            latest_row = conn.execute(
                "SELECT value, timestamp FROM samples "
                "WHERE feature_type = ? ORDER BY timestamp DESC LIMIT 1",
                (feature,),
            ).fetchone()

            if latest_row is None:
                return condition == "absent"

            latest_value = latest_row[0]

            if condition in _SIMPLE_CONDITIONS and _is_stale(latest_row[1], window_min):
                return False

            if latest_value is None:
                return False

            if condition == "gt":
                return latest_value > threshold
            elif condition == "lt":
                return latest_value < threshold
            elif condition == "gte":
                return latest_value >= threshold
            elif condition == "lte":
                return latest_value <= threshold

            since = ts_fmt(datetime.now(timezone.utc) - timedelta(minutes=window_min))
            window_rows = conn.execute(
                "SELECT value FROM samples "
                "WHERE feature_type = ? AND timestamp > ? ORDER BY timestamp",
                (feature, since),
            ).fetchall()

            if condition == "absent":
                return len(window_rows) == 0

            values = [r[0] for r in window_rows if r[0] is not None]

            if not values:
                return False
            if condition in ("spike", "drop", "delta_gt") and len(values) < 2:
                return False

            avg = statistics.mean(values)
            std = statistics.stdev(values) if len(values) >= 2 else 0.0

            if condition == "avg_gt":
                return avg > threshold
            elif condition == "avg_lt":
                return avg < threshold
            elif condition == "spike":
                bound = avg + threshold * std if std > 0 else avg + threshold
                return latest_value > bound
            elif condition == "drop":
                bound = avg - threshold * std if std > 0 else avg - threshold
                return latest_value < bound
            elif condition == "delta_gt":
                previous = values[-2]
                return abs(latest_value - previous) > threshold

        return False


    def _mark_triggered(self, rule_id: int) -> None:
        try:
            with sqlite3.connect(str(self.memory_db_file), timeout=10) as conn:
                conn.execute(
                    "UPDATE trigger_rules SET last_triggered_at = ?, trigger_count = trigger_count + 1 WHERE id = ?",
                    (ts_now(), rule_id),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("Failed to mark trigger rule %d: %s", rule_id, exc)


def insert_default_trigger_rules(memory_db_file: Path) -> None:
    try:
        with sqlite3.connect(str(memory_db_file), timeout=10) as conn:
            count = conn.execute("SELECT COUNT(*) FROM trigger_rules").fetchone()[0]
            if count > 0:
                return

            defaults = [
                (
                    "High resting heart rate",
                    "resting_heart_rate",
                    "gt",
                    85.0,
                    60,
                    120,
                    "Resting heart rate exceeded 85 BPM. Investigate recent cardiovascular data, check for stress signals, and assess whether this is exercise-related or concerning.",
                ),
                (
                    "Low resting heart rate",
                    "resting_heart_rate",
                    "lt",
                    45.0,
                    60,
                    120,
                    "Resting heart rate dropped below 45 BPM. Investigate recent cardiovascular data and assess whether this reflects good fitness or a concerning bradycardia.",
                ),
                (
                    "Low blood oxygen",
                    "blood_oxygen",
                    "lt",
                    0.93,
                    30,
                    180,
                    "Blood oxygen dropped below 93%. Analyze respiratory data, check for breathing disturbances, and assess severity.",
                ),
                (
                    "Heart rate spike",
                    "heart_rate",
                    "spike",
                    2.5,
                    30,
                    90,
                    "Heart rate spiked well above its recent baseline. Check whether this "
                    "lines up with a workout or activity; if the user was at rest, look for "
                    "stress, caffeine, or other causes and advise accordingly.",
                ),
                (
                    "Prolonged elevated heart rate",
                    "heart_rate",
                    "avg_gt",
                    110.0,
                    30,
                    120,
                    "Average heart rate stayed above 110 BPM for half an hour without an "
                    "obvious workout. Check the activity context and assess for stress, "
                    "illness, or overexertion, then advise.",
                ),
                (
                    "Low heart rate variability",
                    "heart_rate_variability",
                    "lt",
                    20.0,
                    180,
                    360,
                    "Heart rate variability dropped to a low level, which can signal stress, "
                    "fatigue, or under-recovery. Review recent sleep, training load, and "
                    "stress, and suggest concrete recovery actions.",
                ),
            ]

            conn.executemany(
                "INSERT INTO trigger_rules (name, feature_type, condition, threshold, window_minutes, cooldown_minutes, prompt_goal) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                defaults,
            )
            conn.commit()
            logger.info("Inserted %d default trigger rules", len(defaults))
    except Exception as exc:
        logger.debug("Failed to insert default trigger rules: %s", exc)
