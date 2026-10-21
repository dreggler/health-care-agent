from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.connection_manager import ConnectionManager
from backend.services.streaming_service import (
    DataStreamingService,
    parse_window_to_minutes,
    shutdown_executor,
)


class TestConnectionManager:

    @pytest.fixture
    def manager(self) -> ConnectionManager:
        return ConnectionManager()

    async def test_connect_adds_to_pool(self, manager):
        ws = AsyncMock()
        await manager.connect(ws)
        assert ws in manager.active_connections
        assert manager.count() == 1
        ws.accept.assert_awaited_once()

    async def test_disconnect_removes_from_pool(self, manager):
        ws = AsyncMock()
        await manager.connect(ws)
        manager.disconnect(ws)
        assert ws not in manager.active_connections
        assert manager.count() == 0

    async def test_disconnect_nonexistent_does_not_raise(self, manager):
        ws = AsyncMock()
        manager.disconnect(ws)

    async def test_is_connected(self, manager):
        ws = AsyncMock()
        assert manager.is_connected(ws) is False
        await manager.connect(ws)
        assert manager.is_connected(ws) is True
        manager.disconnect(ws)
        assert manager.is_connected(ws) is False

    async def test_count(self, manager):
        assert manager.count() == 0
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await manager.connect(ws1)
        assert manager.count() == 1
        await manager.connect(ws2)
        assert manager.count() == 2
        manager.disconnect(ws1)
        assert manager.count() == 1

    async def test_broadcast_sends_to_all(self, manager):
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await manager.connect(ws1)
        await manager.connect(ws2)

        message = {"type": "test", "data": "hello"}
        await manager.broadcast(message)

        ws1.send_json.assert_awaited_once_with(message)
        ws2.send_json.assert_awaited_once_with(message)

    async def test_broadcast_removes_failed_connections(self, manager):
        ws_good = AsyncMock()
        ws_bad = AsyncMock()
        ws_bad.send_json.side_effect = Exception("Connection lost")

        await manager.connect(ws_good)
        await manager.connect(ws_bad)
        assert manager.count() == 2

        await manager.broadcast({"type": "test"})

        assert manager.count() == 1
        assert ws_good in manager.active_connections
        assert ws_bad not in manager.active_connections

    async def test_broadcast_empty_pool(self, manager):
        await manager.broadcast({"type": "test"})

    async def test_multiple_connects_same_ws(self, manager):
        ws = AsyncMock()
        await manager.connect(ws)
        await manager.connect(ws)
        assert manager.count() == 1


class TestParseWindowToMinutes:

    def test_1hour(self):
        assert parse_window_to_minutes("1hour") == 60

    def test_1day(self):
        assert parse_window_to_minutes("1day") == 24 * 60

    def test_1week(self):
        assert parse_window_to_minutes("1week") == 7 * 24 * 60

    def test_1month(self):
        assert parse_window_to_minutes("1month") == 30 * 24 * 60

    def test_default_for_unknown(self):
        assert parse_window_to_minutes("unknown") == 60

    def test_none_defaults(self):
        assert parse_window_to_minutes(None) == 60

    def test_case_insensitive(self):
        assert parse_window_to_minutes("1HOUR") == 60
        assert parse_window_to_minutes("1Day") == 24 * 60


class TestGlobalInstances:

    def test_global_managers_exist(self):
        from backend.services.connection_manager import (
            agent_monitor_manager,
            data_stream_manager,
        )
        assert isinstance(data_stream_manager, ConnectionManager)
        assert isinstance(agent_monitor_manager, ConnectionManager)
        assert data_stream_manager is not agent_monitor_manager

    def test_global_managers_independent(self):
        from backend.services.connection_manager import (
            agent_monitor_manager,
            data_stream_manager,
        )
        assert data_stream_manager.active_connections is not agent_monitor_manager.active_connections


class TestDataStreamingService:

    def test_stream_data_is_static(self):
        assert asyncio.iscoroutinefunction(DataStreamingService.stream_data)

    async def test_stream_data_sends_start_event(self):
        ws = AsyncMock()
        active = {ws}

        mock_reader = MagicMock()
        mock_reader.get_feature_types.return_value = ["heart_rate"]
        import pandas as pd
        mock_reader.load_feature_data.return_value = pd.DataFrame()
        mock_reader.load_features_batch.return_value = pd.DataFrame()

        mock_state = {
            "stream_config": {"granularity": "real-time"},
            "live_history_window": "1hour",
        }

        call_count = 0
        async def _limited_send(msg):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                active.discard(ws)

        ws.send_json = _limited_send

        with patch("backend.services.streaming_service.create_reader", return_value=mock_reader), \
             patch("backend.services.streaming_service.settings", MagicMock(DATA_STORE_PATH="/tmp/test")), \
             patch("backend.api.config_routes.get_app_state", return_value=mock_state):
            try:
                await asyncio.wait_for(
                    DataStreamingService.stream_data(
                        ws, ["LiveUser"], {"granularity": "real-time"}, active
                    ),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                pass

        assert call_count >= 1


class TestShutdownExecutor:

    def test_shutdown_does_not_raise(self):
        shutdown_executor()
