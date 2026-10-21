from __future__ import annotations

import pytest

from backend.agent.memory_manager import MemoryManager
from backend.ios_gateway import IOSGateway
from backend.ios_gateway.image_store import image_store


class _FakeAgent:
    def __init__(self):
        self.events = []

    async def _emit(self, ev):
        self.events.append(ev)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "DATA_STORE_PATH", str(tmp_path / "data"))
    uid = "LiveUser"
    mm = MemoryManager(tmp_path / "memory", uid)
    return uid, mm, tmp_path


async def test_agent_image_survives_reload(env, monkeypatch):
    uid, mm, tmp_path = env

    import backend.api.agent_state as agent_state
    fa = _FakeAgent()
    monkeypatch.setitem(agent_state.active_agents, uid, {"agent": fa})

    chart = tmp_path / "trend.png"
    chart.write_bytes(b"\x89PNG real-chart-bytes")
    gw = IOSGateway(uid)
    ok = await gw.send_photo(str(chart), caption="weekly trend", chat_id=uid)
    assert ok and gw.last_image_id

    image_id = gw.last_image_id
    assert fa.events[0]["url"] == f"/api/agent/chat-image/{image_id}"

    img_md = f"![chart](/api/agent/chat-image/{image_id})"
    await mm.persist_chat_turn(
        f"ios:{uid}", "assistant", f"weekly trend\n\n{img_md}",
        message_hash="h1234567", image_id=image_id,
    )

    image_store._items.clear()
    mm2 = MemoryManager(tmp_path / "memory", uid)
    rows = mm2.get_chat_history(f"ios:{uid}")
    assert len(rows) == 1
    assert img_md in rows[0]["content"]
    assert rows[0]["image_id"] == image_id

    path = image_store.get(image_id, uid) or image_store.durable_path(image_id, uid)
    assert path is not None
    with open(path, "rb") as f:
        assert f.read() == b"\x89PNG real-chart-bytes"
    assert image_store.durable_path(image_id, "intruder") is None
