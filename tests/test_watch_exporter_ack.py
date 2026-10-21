from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("rich")
from aiohttp.test_utils import TestClient, TestServer

SERVER_PY = Path(__file__).resolve().parent.parent / "ios" / "Server" / "server.py"


@pytest.fixture()
def server_mod():
    pytest.importorskip("rich")
    spec = importlib.util.spec_from_file_location("watch_exporter_server", SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._auth_token = ""
    mod._sync_enabled = True
    return mod


def _row_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as con:
        return con.execute("SELECT COUNT(*) FROM health_samples_eav").fetchone()[0]


@pytest.mark.asyncio
async def test_hello_ack_and_nack(server_mod, tmp_path):
    db = tmp_path / "watch.db"
    app = server_mod.make_app(str(db))
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/ws")

        hello = await ws.receive_json()
        assert hello == {"type": "hello", "ack": 1}

        batch = {"id": "b1", "items": [{"ts": 1_700_000_000, "f": "heart_rate", "v": 70}]}
        await ws.send_bytes(json.dumps(batch).encode())
        ack = await ws.receive_json()
        assert ack["type"] == "ack" and ack["id"] == "b1" and ack["n"] == 1

        await ws.send_str(json.dumps([{"ts": 1_700_000_001, "f": "heart_rate", "v": 71}]))
        ack = await ws.receive_json()
        assert ack["type"] == "ack" and "id" not in ack

        server_mod._sync_enabled = False
        await ws.send_bytes(json.dumps(
            {"id": "b2", "items": [{"ts": 1_700_000_002, "f": "heart_rate", "v": 72}]}
        ).encode())
        nack = await ws.receive_json()
        assert nack == {"type": "nack", "reason": "sync_disabled", "id": "b2"}

        server_mod._sync_enabled = True
        await ws.send_str("not json")
        bad = await ws.receive_json()
        assert bad["type"] == "ack" and bad["n"] == 0

        await ws.close()

    assert _row_count(db) == 2
