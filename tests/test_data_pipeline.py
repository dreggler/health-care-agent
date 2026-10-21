from __future__ import annotations

import json
import sqlite3
import time

import pandas as pd
import pytest

from backend.agent.data_store import DataStore
from backend.agent.memory_manager import MemoryManager, _ensure_schema
from backend.data_readers.data_store_reader import DataStoreReader
from backend.data_readers.watch_db_reader import WatchDBReader


class TestWatchDBReader:

    @pytest.fixture
    def reader(self, tmp_dirs, watch_db) -> WatchDBReader:
        return WatchDBReader(tmp_dirs["watch"])

    def test_init_creates_directory(self, tmp_path):
        new_dir = tmp_path / "nonexistent"
        WatchDBReader(new_dir)
        assert new_dir.exists()

    def test_get_available_users(self, reader):
        assert reader.get_available_users() == ["LiveUser"]

    def test_get_feature_types_with_db(self, reader):
        features = reader.get_feature_types()
        assert isinstance(features, list)
        assert len(features) > 0
        for expected in ["heart_rate", "steps", "active_energy"]:
            assert expected in features

    def test_get_feature_types_without_db(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        reader = WatchDBReader(empty_dir)
        features = reader.get_feature_types()
        assert "heart_rate" in features
        assert "steps" in features
        assert len(features) > 20

    def test_get_feature_columns(self, reader):
        assert reader.get_feature_columns("heart_rate") == ["value"]

    def test_get_date_range(self, reader):
        min_dt, max_dt = reader.get_date_range(["LiveUser"])
        assert min_dt < max_dt
        assert isinstance(min_dt, pd.Timestamp)

    def test_get_date_range_missing_db(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        reader = WatchDBReader(empty_dir)
        min_dt, max_dt = reader.get_date_range(["LiveUser"])
        assert max_dt > min_dt

    def test_load_feature_data(self, reader):
        df = reader.load_feature_data(
            ["LiveUser"], "heart_rate", minutes=1440
        )
        assert isinstance(df, pd.DataFrame)
        if not df.empty:
            assert "date" in df.columns
            assert "value" in df.columns
            assert "feature_type" in df.columns
            assert "pid" in df.columns
            assert "ts" in df.columns

    def test_load_feature_data_missing_db(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        reader = WatchDBReader(empty_dir)
        df = reader.load_feature_data(["LiveUser"], "heart_rate")
        assert df.empty

    def test_get_all_samples_since_id(self, reader):
        samples = reader.get_all_samples_since_id(0)
        assert isinstance(samples, list)
        assert len(samples) > 0
        assert "id" in samples[0]
        assert "feature_type" in samples[0]
        assert "ts" in samples[0]
        assert "value" in samples[0]

        high_id = samples[-1]["id"]
        later_samples = reader.get_all_samples_since_id(high_id)
        assert len(later_samples) == 0

    def test_get_all_samples_since(self, reader):
        samples = reader.get_all_samples_since(0.0)
        assert len(samples) > 0

    def test_get_latest_samples(self, reader):
        latest = reader.get_latest_samples()
        assert isinstance(latest, dict)
        assert len(latest) > 0
        for _feature, info in latest.items():
            assert "ts" in info
            assert "value" in info

    def test_load_feature_data_with_since_ts(self, reader):
        all_data = reader.load_feature_data(["LiveUser"], "heart_rate", minutes=1440)
        if not all_data.empty:
            mid_ts = all_data["ts"].median()
            filtered = reader.load_feature_data(
                ["LiveUser"], "heart_rate", since_ts=mid_ts
            )
            assert len(filtered) <= len(all_data)


class TestDataStoreReader:

    @pytest.fixture
    def reader(self, tmp_dirs, health_data_db) -> DataStoreReader:
        return DataStoreReader(tmp_dirs["data_stores"])

    def test_get_available_users(self, reader):
        pids = reader.get_available_users()
        assert "LiveUser" in pids

    def test_get_feature_types(self, reader):
        features = reader.get_feature_types()
        assert isinstance(features, list)
        assert len(features) > 0
        assert "heart_rate" in features
        assert "steps" in features

    def test_get_feature_columns(self, reader):
        assert reader.get_feature_columns("heart_rate") == ["value"]

    def test_get_date_range(self, reader):
        min_dt, max_dt = reader.get_date_range(["LiveUser"])
        assert min_dt is not None
        assert max_dt is not None

    def test_load_feature_data(self, reader):
        df = reader.load_feature_data(
            ["LiveUser"], "heart_rate", minutes=1500
        )
        assert isinstance(df, pd.DataFrame)
        if not df.empty:
            assert "date" in df.columns
            assert "value" in df.columns
            assert "feature_type" in df.columns
            assert "pid" in df.columns
            assert "ts" in df.columns

    def test_load_feature_data_empty_pids(self, reader):
        df = reader.load_feature_data([], "heart_rate")
        assert df.empty

    def test_load_features_batch(self, reader):
        df = reader.load_features_batch(
            ["LiveUser"],
            ["heart_rate", "steps"],
            minutes=1500,
        )
        assert isinstance(df, pd.DataFrame)
        if not df.empty:
            assert set(df["feature_type"].unique()) <= {"heart_rate", "steps"}

    def test_load_features_batch_with_since_ts(self, reader):
        df = reader.load_features_batch(
            ["LiveUser"],
            ["heart_rate"],
            since_ts=time.time() - 86400,
        )
        assert isinstance(df, pd.DataFrame)

    def test_handles_missing_db_gracefully(self, tmp_path):
        data_dir = tmp_path / "data_stores"
        data_dir.mkdir()
        reader = DataStoreReader(data_dir)
        df = reader.load_feature_data(["NewUser"], "heart_rate")
        assert isinstance(df, pd.DataFrame)
        assert df.empty


class TestDataStore:

    @pytest.fixture
    def store(self, tmp_dirs) -> DataStore:
        return DataStore(db_path=tmp_dirs["data_stores"], user_id="TestUser")

    def test_init_creates_db(self, store, tmp_dirs):
        db_file = tmp_dirs["data_stores"] / "TestUser_data.db"
        assert db_file.exists()

    def test_init_creates_tables(self, store):
        conn = store.get_connection()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        table_names = [t[0] for t in tables]
        assert "samples" in table_names
        assert "store_metadata" in table_names

    def test_ingest_batch_feature_type_format(self, store):
        batch = {
            "data": [
                {
                    "date": "2026-03-20T10:00:00",
                    "value": 72.5,
                    "feature_type": "heart_rate",
                    "pid": "TestUser",
                },
                {
                    "date": "2026-03-20T10:05:00",
                    "value": 150.0,
                    "feature_type": "steps",
                    "pid": "TestUser",
                },
            ],
            "data_timestamp": "2026-03-20T10:05:00",
            "num_records": 2,
            "is_live": True,
        }
        store.ingest_batch(batch)

        stats = store.get_stats()
        assert stats["total_records"] == 2
        assert "heart_rate" in stats["by_feature"]
        assert "steps" in stats["by_feature"]

    def test_ingest_batch_wide_format(self, store):
        batch = {
            "data": [
                {
                    "date": "2026-03-20T11:00:00",
                    "heart_rate": 75.0,
                    "steps": 200.0,
                    "pid": "TestUser",
                },
            ],
            "data_timestamp": "2026-03-20T11:00:00",
        }
        store.ingest_batch(batch)
        stats = store.get_stats()
        assert stats["total_records"] >= 2

    def test_ingest_batch_skips_none_values(self, store):
        batch = {
            "data": [
                {
                    "date": "2026-03-20T12:00:00",
                    "value": None,
                    "feature_type": "heart_rate",
                    "pid": "TestUser",
                },
            ],
        }
        store.ingest_batch(batch)
        stats = store.get_stats()
        assert stats["total_records"] == 0

    def test_ingest_batch_empty_data(self, store):
        store.ingest_batch({"data": [], "data_timestamp": None})
        stats = store.get_stats()
        assert stats["total_records"] == 0

    def test_save_and_get_ingestion_progress(self, store):
        ts = "2026-03-20T10:00:00"
        store.save_ingestion_progress(ts)
        assert store.get_last_ingestion_time() == ts

    def test_save_and_get_ingestion_id(self, store):
        store.save_ingestion_id(12345)
        assert store.get_last_ingested_id() == 12345

    def test_get_last_ingested_id_default(self, store):
        assert store.get_last_ingested_id() == 0

    def test_query(self, store):
        store.ingest_batch({
            "data": [
                {
                    "date": "2026-03-20T13:00:00",
                    "value": 80.0,
                    "feature_type": "heart_rate",
                    "pid": "TestUser",
                },
            ],
        })
        df = store.query("SELECT * FROM samples WHERE feature_type = 'heart_rate'")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 1
        assert df.iloc[0]["value"] == 80.0

    def test_get_stats_empty(self, store):
        stats = store.get_stats()
        assert stats["total_records"] == 0
        assert stats["by_feature"] == {}

    def test_get_stats_with_data(self, store):
        records = [
            {"date": f"2026-03-20T{h:02d}:00:00", "value": 70.0 + h,
             "feature_type": "heart_rate", "pid": "TestUser"}
            for h in range(10)
        ]
        store.ingest_batch({"data": records})
        stats = store.get_stats()
        assert stats["total_records"] == 10
        assert stats["by_feature"]["heart_rate"] == 10
        assert stats["time_range"]["min"] is not None
        assert stats["time_range"]["max"] is not None

    def test_stop_ingestion(self, store):
        store.is_ingesting = True
        store.stop_ingestion()
        assert store.is_ingesting is False

    def test_get_connection(self, store):
        conn = store.get_connection()
        result = conn.execute("SELECT 1").fetchone()
        conn.close()
        assert result[0] == 1

    def test_ingest_batch_dedup(self, store):
        batch1 = {
            "data": [
                {"date": "2026-03-20T14:00:00", "value": 65.0,
                 "feature_type": "heart_rate", "pid": "TestUser"},
            ],
        }
        batch2 = {
            "data": [
                {"date": "2026-03-20T14:00:00", "value": 99.0,
                 "feature_type": "heart_rate", "pid": "TestUser"},
            ],
        }
        store.ingest_batch(batch1)
        store.ingest_batch(batch2)
        stats = store.get_stats()
        assert stats["total_records"] == 1
        df = store.query("SELECT value FROM samples WHERE feature_type = 'heart_rate'")
        assert df.iloc[0]["value"] == 99.0


class TestMemoryManager:

    def test_init_creates_db(self, tmp_path):
        db_dir = tmp_path / "memory"
        db_dir.mkdir()
        mm = MemoryManager(db_dir, "TestUser")
        assert mm.db_file.exists()

        with sqlite3.connect(mm.db_file) as conn:
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        for expected in ("reports", "activity_log", "scheduled_tasks", "personalised_pages"):
            assert expected in tables, f"Missing table: {expected}"

    def test_get_recent_reports(self, memory_manager):
        reports = memory_manager.get_recent_reports(limit=10)
        assert len(reports) >= 1
        assert reports[0]["title"] == "Test Report"
        assert reports[0]["alert_level"] == "normal"

    def test_get_recent_reports_with_metadata(self, memory_manager):
        with sqlite3.connect(memory_manager.db_file) as conn:
            conn.execute(
                "INSERT INTO reports (title, content, alert_level, metadata) VALUES (?, ?, ?, ?)",
                ("Meta Report", "Content", "info", json.dumps({"key": "value"})),
            )
            conn.commit()
        reports = memory_manager.get_recent_reports(limit=10)
        meta_report = next(r for r in reports if r["title"] == "Meta Report")
        assert isinstance(meta_report["metadata"], dict)
        assert meta_report["metadata"]["key"] == "value"

    def test_get_recent_activity(self, memory_manager):
        events = memory_manager.get_recent_activity(limit=100)
        assert len(events) >= 5
        assert events[0]["type"] == "test_event_0"

    async def test_persist_activity(self, memory_manager):
        event = {"type": "test_persist", "data": "async persistence test"}
        await memory_manager.persist_activity(event)

        events = memory_manager.get_recent_activity(limit=100)
        types = [e["type"] for e in events]
        assert "test_persist" in types

    async def test_activity_log_trimming(self, tmp_path):
        db_dir = tmp_path / "memory"
        db_dir.mkdir()
        mm = MemoryManager(db_dir, "TrimUser")

        original_limit = mm.ACTIVITY_LIMIT
        mm.ACTIVITY_LIMIT = 10

        try:
            for i in range(15):
                await mm.persist_activity({"type": f"event_{i}", "index": i})

            with sqlite3.connect(mm.db_file) as conn:
                count = conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
            assert count <= 10
        finally:
            mm.ACTIVITY_LIMIT = original_limit

    def test_get_stats(self, memory_manager):
        stats = memory_manager.get_stats()
        assert "table_counts" in stats
        assert "date_range" in stats
        assert "reports" in stats["table_counts"]
        assert stats["table_counts"]["reports"] >= 1

    def test_ensure_schema_idempotent(self, memory_manager):
        with sqlite3.connect(memory_manager.db_file) as conn:
            _ensure_schema(conn)
            _ensure_schema(conn)
            _ensure_schema(conn)

    def test_multiple_managers_same_db(self, tmp_path):
        db_dir = tmp_path / "memory"
        db_dir.mkdir()
        mm1 = MemoryManager(db_dir, "SharedUser")
        mm2 = MemoryManager(db_dir, "SharedUser")

        with sqlite3.connect(mm1.db_file) as conn:
            conn.execute(
                "INSERT INTO reports (title, content) VALUES (?, ?)",
                ("Cross-instance", "Test content"),
            )
            conn.commit()

        reports = mm2.get_recent_reports()
        assert any(r["title"] == "Cross-instance" for r in reports)
