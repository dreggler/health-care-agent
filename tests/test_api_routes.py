from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch


class TestAgentStatusRoute:

    async def test_status_no_agents(self, test_client):
        with patch("backend.api.agent_state.active_agents", {}):
            resp = await test_client.get("/api/agent/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["active_agents"] == 0

    async def test_status_with_specific_user_not_found(self, test_client):
        with patch("backend.api.agent_state.active_agents", {}):
            resp = await test_client.get("/api/agent/status", params={"user_id": "nobody"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False

    async def test_status_with_running_agent(self, test_client, mock_agent):
        agents = {
            "LiveUser": {
                "agent": mock_agent,
                "config": {
                    "llm_provider": "gemini",
                    "model": "gemini-3.1-flash-lite-preview",
                    "granularity": "real-time",
                    "speed_multiplier": 1.0,
                },
            }
        }
        with patch("backend.api.agent_lifecycle.active_agents", agents), \
             patch("backend.api.agent_state.active_agents", agents):
            resp = await test_client.get(
                "/api/agent/status", params={"user_id": "LiveUser"}
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["running"] is True
        assert "status" in data
        assert data["config"]["llm_provider"] == "gemini"


class TestAgentStartRoute:

    async def test_start_rejects_when_agent_already_running(self, test_client, mock_agent):
        agents = {"LiveUser": {"agent": mock_agent}}
        with patch("backend.api.agent_lifecycle.active_agents", agents), \
             patch("backend.api.agent_state.active_agents", agents):
            resp = await test_client.post(
                "/api/agent/start",
                json={"user_id": "LiveUser", "llm_provider": "gemini"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "already running" in data["error"]

    async def test_start_rejects_different_user_when_one_running(
        self, test_client, mock_agent
    ):
        agents = {"LiveUser": {"agent": mock_agent}}
        with patch("backend.api.agent_lifecycle.active_agents", agents), \
             patch("backend.api.agent_state.active_agents", agents):
            resp = await test_client.post(
                "/api/agent/start",
                json={"user_id": "OtherUser", "llm_provider": "gemini"},
            )
        data = resp.json()
        assert data["success"] is False
        assert "LiveUser" in data["error"]


class TestAgentStopRoute:

    async def test_stop_nonexistent_agent(self, test_client):
        with patch("backend.api.agent_lifecycle.active_agents", {}), \
             patch("backend.api.agent_state.active_agents", {}):
            resp = await test_client.post(
                "/api/agent/stop", params={"user_id": "nobody"}
            )
        assert resp.status_code == 404

    async def test_stop_running_agent(self, test_client, mock_agent):
        async def _noop():
            pass

        task = asyncio.create_task(_noop())
        await task

        agents = {
            "LiveUser": {
                "agent": mock_agent,
                "task": task,
                "ingest_task": task,
                "data_store": MagicMock(),
                "config": {},
                "memory": MagicMock(),
                "event_queue": asyncio.Queue(),
            }
        }
        with patch("backend.api.agent_lifecycle.active_agents", agents), \
             patch("backend.api.agent_state.active_agents", agents):
            resp = await test_client.post(
                "/api/agent/stop", params={"user_id": "LiveUser"}
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        mock_agent.stop.assert_called_once()

    async def test_stop_toctou_race_condition(self, test_client):
        with patch("backend.api.agent_lifecycle.active_agents", {}), \
             patch("backend.api.agent_state.active_agents", {}):
            resp = await test_client.post(
                "/api/agent/stop", params={"user_id": "LiveUser"}
            )
        assert resp.status_code == 404


class TestQuickAnalysisRoute:

    async def test_quick_analysis_no_agent(self, test_client):
        with patch("backend.api.agent_lifecycle.active_agents", {}), \
             patch("backend.api.agent_state.active_agents", {}):
            resp = await test_client.post("/api/agent/quick-analysis")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "neutral"
        assert "not running" in data["message"].lower() or "start" in data["message"].lower()

    async def test_quick_analysis_success(self, test_client, mock_agent):
        agents = {
            "LiveUser": {
                "agent": mock_agent,
                "config": {},
            }
        }
        with patch("backend.api.agent_lifecycle.active_agents", agents), \
             patch("backend.api.agent_state.active_agents", agents):
            resp = await test_client.post("/api/agent/quick-analysis")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "happy"
        assert "looks good" in data["message"].lower()


class TestConfigRoutes:

    async def test_get_stream_config(self, test_client):
        resp = await test_client.get("/api/config/stream")
        assert resp.status_code == 200
        data = resp.json()
        assert data["granularity"] == "real-time"
        assert "is_streaming" in data
        assert data["selected_users"] == ["LiveUser"]

    async def test_post_stream_config(self, test_client):
        resp = await test_client.post(
            "/api/config/stream",
            json={"granularity": "real-time", "is_streaming": True, "live_history_window": "1day"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    async def test_get_users(self, test_client):
        resp = await test_client.get("/api/config/users")
        assert resp.status_code == 200
        data = resp.json()
        assert data["users"] == ["LiveUser"]

    async def test_post_users_noop(self, test_client):
        resp = await test_client.post("/api/config/users", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["users"] == ["LiveUser"]

    async def test_get_defaults(self, test_client):
        resp = await test_client.get("/api/config/defaults")
        assert resp.status_code == 200
        data = resp.json()
        assert "llm_provider" in data
        assert "model" in data
        assert data["data_source"] == "live"


class TestDataRoutes:

    async def test_get_feature_types(self, test_client, mock_settings, health_data_db):
        mock_reader = MagicMock()
        mock_reader.get_feature_types.return_value = ["heart_rate", "steps", "active_energy"]

        with patch("backend.api.data_routes._reader", mock_reader):
            resp = await test_client.get("/api/data/feature_types")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "heart_rate" in data["feature_types"]

    async def test_get_data_source(self, test_client):
        resp = await test_client.get("/api/data/source")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data_source"] == "live"

    async def test_get_feature_metadata(self, test_client):
        resp = await test_client.get("/api/data/feature_metadata")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "features" in data

    async def test_get_dashboard_data(self, test_client):
        import pandas as pd

        mock_reader = MagicMock()
        mock_reader.get_feature_types.return_value = ["heart_rate"]
        mock_df = pd.DataFrame({
            "ts": [1711000000.0, 1711000300.0],
            "value": [72.0, 75.0],
            "feature_type": ["heart_rate", "heart_rate"],
        })
        mock_reader.load_feature_data.return_value = mock_df

        with patch("backend.api.data_routes._reader", mock_reader):
            resp = await test_client.get("/api/data/dashboard", params={"minutes": 60})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "features" in data


class TestPromptRoutes:

    async def test_list_prompts(self, test_client, prompt_files):
        with patch("backend.api.prompt_routes.PROMPTS_DIR", prompt_files):
            resp = await test_client.get("/api/prompts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        ids = [p["id"] for p in data["prompts"]]
        assert "soul" in ids
        assert "data_schema" in ids
        assert "memory_guide" in ids
        assert "experience" in ids
        assert "user" in ids

    async def test_get_prompt(self, test_client, prompt_files):
        with patch("backend.api.prompt_routes.PROMPTS_DIR", prompt_files):
            resp = await test_client.get("/api/prompts/soul")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "HIME" in data["content"]

    async def test_get_nonexistent_prompt(self, test_client):
        resp = await test_client.get("/api/prompts/unknown")
        assert resp.status_code == 404

    async def test_update_prompt(self, test_client, prompt_files):
        new_content = "Updated experience content with new patterns."
        with patch("backend.api.prompt_routes.PROMPTS_DIR", prompt_files):
            resp = await test_client.post(
                "/api/prompts/experience",
                json={"content": new_content},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

        written = (prompt_files / "experience.md").read_text(encoding="utf-8")
        assert new_content in written

    async def test_update_nonexistent_prompt(self, test_client):
        resp = await test_client.post(
            "/api/prompts/unknown",
            json={"content": "something"},
        )
        assert resp.status_code == 404


class TestRootEndpoints:
    async def test_root(self, test_client):
        resp = await test_client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert "version" in data

    async def test_health(self, test_client):
        resp = await test_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"


class TestStreamConnectionsRoute:
    async def test_get_connections(self, test_client):
        resp = await test_client.get("/api/stream/connections")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "data_stream_active_connections" in data
        assert "agent_monitor_active_connections" in data


class TestLastConfigRoute:
    async def test_last_config_no_file(self, test_client, mock_settings):
        with patch("backend.api.agent_lifecycle.settings", mock_settings):
            resp = await test_client.get("/api/agent/last-config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False

    async def test_last_config_with_file(self, test_client, mock_settings):
        cfg = {
            "user_id": "LiveUser",
            "llm_provider": "gemini",
            "model": "gemini-3.1-flash-lite-preview",
        }
        mock_settings.AGENT_LAST_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        mock_settings.AGENT_LAST_CONFIG_PATH.write_text(json.dumps(cfg))

        with patch("backend.api.agent_lifecycle.settings", mock_settings):
            resp = await test_client.get("/api/agent/last-config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["config"]["user_id"] == "LiveUser"
