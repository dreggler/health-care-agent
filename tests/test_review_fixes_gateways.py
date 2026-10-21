from __future__ import annotations

import asyncio
import base64
import builtins
import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from backend.feishu.transport import (
    FeishuWebhookTransport,
    _event_to_envelope,
    decrypt_feishu_payload,
)


async def _noop_async(*args: Any, **kwargs: Any) -> None:
    return None


def _make_transport(
    on_message=_noop_async,
    on_card_action=_noop_async,
    *,
    encrypt_key: str = "",
    verification_token: str = "tk-1",
    allowed: set[str] | None = None,
) -> FeishuWebhookTransport:
    return FeishuWebhookTransport(
        on_message=on_message,
        on_card_action=on_card_action,
        webhook_path="/api/feishu/webhook",
        verification_token=verification_token,
        encrypt_key=encrypt_key,
        allowed_chat_ids=allowed if allowed is not None else {"oc_allowed"},
    )


def _feishu_encrypt(encrypt_key: str, payload: dict) -> str:
    import os

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv = os.urandom(16)
    plain = json.dumps(payload).encode("utf-8")
    pad = 16 - (len(plain) % 16)
    plain += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + encryptor.update(plain) + encryptor.finalize()).decode()


def _message_event(chat_id: str = "oc_allowed", event_id: str = "e1") -> dict:
    return {
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": event_id,
            "token": "tk-1",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_1",
                "chat_id": chat_id,
                "message_type": "text",
                "content": json.dumps({"text": "hi"}),
            },
        },
    }


def test_decrypt_feishu_payload_roundtrip() -> None:
    payload = {"type": "url_verification", "challenge": "chal-9", "token": "tk-1"}
    blob = _feishu_encrypt("my-encrypt-key", payload)
    assert decrypt_feishu_payload("my-encrypt-key", blob) == payload


def test_decrypt_rejects_wrong_key() -> None:
    blob = _feishu_encrypt("right-key", {"a": 1})
    with pytest.raises(ValueError):
        decrypt_feishu_payload("wrong-key-xxxx", blob)


def test_decrypt_requires_configured_key() -> None:
    with pytest.raises(ValueError):
        decrypt_feishu_payload("", "anything")


def test_decrypt_works_without_the_cryptography_package(monkeypatch) -> None:
    pytest.importorskip("Crypto.Cipher.AES")
    payload = {"header": {"event_type": "im.message.receive_v1"}}
    blob = _feishu_encrypt("k-1", payload)

    real_import = builtins.__import__

    def _no_cryptography(name, *args, **kwargs):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ImportError("simulated: cryptography is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_cryptography)
    assert decrypt_feishu_payload("k-1", blob) == payload


async def test_webhook_accepts_signed_encrypted_message_event() -> None:
    pytest.importorskip("fastapi")
    import httpx
    from fastapi import FastAPI

    received: list[Any] = []

    async def capture(envelope):
        received.append(envelope)

    app = FastAPI()
    transport = _make_transport(on_message=capture, encrypt_key="enc-key-abc")
    transport.register_routes(app)

    blob = _feishu_encrypt("enc-key-abc", _message_event(event_id="enc-1"))
    raw = json.dumps({"encrypt": blob}).encode()
    ts = str(int(time.time()))
    sig = hashlib.sha256((ts + "n1" + "enc-key-abc").encode() + raw).hexdigest()

    asgi = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
        resp = await client.post(
            "/api/feishu/webhook",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Request-Nonce": "n1",
                "X-Lark-Signature": sig,
            },
        )
        assert resp.status_code == 200, resp.text
        for _ in range(5):
            if received:
                break
            await asyncio.sleep(0)
    assert [e.content for e in received] == ["hi"]


async def test_webhook_answers_encrypted_url_verification() -> None:
    pytest.importorskip("fastapi")
    import httpx
    from fastapi import FastAPI

    app = FastAPI()
    transport = _make_transport(encrypt_key="enc-key-abc")
    transport.register_routes(app)

    blob = _feishu_encrypt(
        "enc-key-abc",
        {"type": "url_verification", "challenge": "chal-enc", "token": "tk-1"},
    )
    asgi = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
        resp = await client.post("/api/feishu/webhook", json={"encrypt": blob})
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "chal-enc"}


async def test_webhook_rejects_undecryptable_payload() -> None:
    pytest.importorskip("fastapi")
    import httpx
    from fastapi import FastAPI

    app = FastAPI()
    transport = _make_transport(encrypt_key="enc-key-abc")
    transport.register_routes(app)

    asgi = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
        resp = await client.post(
            "/api/feishu/webhook", json={"encrypt": "not-really-ciphertext"},
        )
    assert resp.status_code == 400


def test_event_post_without_token_is_rejected() -> None:
    transport = _make_transport(verification_token="tk-1")
    forged = _message_event()
    forged["header"].pop("token")
    assert transport.verify_body_token(forged) is False


def test_event_post_with_matching_token_is_accepted() -> None:
    transport = _make_transport(verification_token="tk-1")
    assert transport.verify_body_token(_message_event()) is True


def test_legacy_card_callback_without_token_still_accepted() -> None:
    transport = _make_transport(verification_token="tk-1")
    assert transport.verify_body_token({"action": {"tag": "button"}}) is True
    assert transport.verify_body_token({}) is True


def test_duplicate_event_id_is_dropped() -> None:
    received: list[Any] = []

    async def capture(envelope):
        received.append(envelope)

    transport = _make_transport(on_message=capture)

    async def _run():
        await transport.dispatch_event(_message_event(event_id="dup-1"))
        await transport.dispatch_event(_message_event(event_id="dup-1"))
        await transport.dispatch_event(_message_event(event_id="dup-2"))

    asyncio.run(_run())
    assert len(received) == 2, "Feishu's retry of the same event_id must be dropped"


def test_seen_event_ids_map_is_bounded() -> None:
    transport = _make_transport()
    cap = transport._MAX_SEEN

    for i in range(cap * 4):
        assert transport._is_duplicate(f"ev-{i}") is False
        assert len(transport._seen_event_ids) <= cap + 1

    assert transport._is_duplicate(f"ev-{cap * 4 - 1}") is True
    before = len(transport._seen_event_ids)
    assert transport._is_duplicate("") is False
    assert transport._is_duplicate("") is False
    assert len(transport._seen_event_ids) == before


async def test_message_event_is_acked_before_processing() -> None:
    pytest.importorskip("fastapi")
    import httpx
    from fastapi import FastAPI

    started = asyncio.Event()
    release = asyncio.Event()
    done: list[Any] = []

    async def slow(envelope):
        started.set()
        await release.wait()
        done.append(envelope)

    app = FastAPI()
    transport = _make_transport(on_message=slow)
    transport.register_routes(app)

    asgi = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
        try:
            resp = await asyncio.wait_for(
                client.post("/api/feishu/webhook", json=_message_event()),
                timeout=2,
            )
        except asyncio.TimeoutError:
            release.set()
            pytest.fail("webhook did not ack before running the handler")
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "ok"}
        await asyncio.wait_for(started.wait(), timeout=1)
        assert done == []
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert len(done) == 1


def test_verify_timestamp_window() -> None:
    transport = _make_transport(encrypt_key="enc-key-abc")
    assert transport.verify_timestamp(str(int(time.time()))) is True
    assert transport.verify_timestamp(str(int(time.time()) - 10_000)) is False
    assert transport.verify_timestamp("") is False
    assert transport.verify_timestamp("not-a-number") is False


async def test_replayed_signed_request_is_rejected() -> None:
    pytest.importorskip("fastapi")
    import httpx
    from fastapi import FastAPI

    app = FastAPI()
    transport = _make_transport(encrypt_key="enc-key-abc")
    transport.register_routes(app)

    body = json.dumps(_message_event()).encode()
    stale_ts = str(int(time.time()) - 86_400)
    sig = hashlib.sha256(
        (stale_ts + "nonce" + "enc-key-abc").encode() + body,
    ).hexdigest()

    asgi = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
        resp = await client.post(
            "/api/feishu/webhook",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Lark-Request-Timestamp": stale_ts,
                "X-Lark-Request-Nonce": "nonce",
                "X-Lark-Signature": sig,
            },
        )
    assert resp.status_code == 401


def test_envelope_timestamp_uses_create_time_utc() -> None:
    event = _message_event()
    event["event"]["message"]["create_time"] = "1700000000000"
    env = _event_to_envelope(event)
    assert env is not None
    assert env.timestamp.tzinfo is not None
    assert env.timestamp.timestamp() == pytest.approx(1_700_000_000)


def test_envelope_timestamp_aware_without_create_time() -> None:
    env = _event_to_envelope(_message_event())
    assert env is not None and env.timestamp.tzinfo is not None


async def test_ws_transport_retries_after_failure(monkeypatch) -> None:
    from backend.feishu import transport as tmod
    from backend.feishu.transport import FeishuWsTransport

    monkeypatch.setattr(tmod, "_WS_BACKOFF_MIN", 0.001)
    monkeypatch.setattr(tmod, "_WS_BACKOFF_MAX", 0.001)

    ws = FeishuWsTransport("app", "secret", _noop_async, _noop_async)
    attempts = 0
    third = asyncio.Event()

    async def flaky(_lark):
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            third.set()
            await asyncio.sleep(3600)
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ws, "_connect_and_serve", flaky)
    monkeypatch.setitem(__import__("sys").modules, "lark_oapi", object())

    await ws.start()
    try:
        await asyncio.wait_for(third.wait(), timeout=5)
    finally:
        await ws.stop()
    assert attempts >= 3, "a failed connect must be retried, not fatal"


async def test_ws_watch_link_detects_a_dead_socket(monkeypatch) -> None:
    from backend.feishu import transport as tmod
    from backend.feishu.transport import FeishuWsTransport

    monkeypatch.setattr(tmod, "_WS_HEALTH_POLL_S", 0.005)
    monkeypatch.setattr(tmod, "_WS_DEAD_AFTER_S", 0.02)

    class _FakeClient:
        _conn = object()

    ws = FeishuWsTransport("app", "secret", _noop_async, _noop_async)
    ws._client = _FakeClient()

    watcher = asyncio.create_task(ws._watch_link())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(watcher), timeout=0.1)

    _FakeClient._conn = None
    await asyncio.wait_for(watcher, timeout=2)


async def test_allowed_updates_is_json_array() -> None:
    from backend.telegram.poller import TelegramPoller

    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"ok": True, "result": []}

    class _FakeClient:
        async def get(self, url, params=None):
            captured.update(params or {})
            return _FakeResp()

    poller = TelegramPoller("tok", _noop_async)
    poller._client = _FakeClient()
    await poller._fetch_updates()

    assert captured["allowed_updates"] == '["message", "callback_query"]'
    assert json.loads(captured["allowed_updates"]) == ["message", "callback_query"]


def test_table_content_is_not_inline_formatted() -> None:
    from backend.telegram.sender import _markdown_to_telegram_html

    md = "| metric | note |\n| --- | --- |\n| HR | *elevated* and _odd_ |"
    html = _markdown_to_telegram_html(md)
    assert "<pre>" in html
    assert "<b>" not in html
    assert "<i>" not in html
    assert "*elevated*" in html


def test_code_block_still_protected() -> None:
    from backend.telegram.sender import _markdown_to_telegram_html

    html = _markdown_to_telegram_html("```\nx = *y*\n```")
    assert "<pre>" in html and "<b>" not in html


def test_truncation_never_splits_a_tag() -> None:
    from backend.telegram.sender import _truncate_telegram_html

    text = "<b>" + ("a" * 200) + "</b>"
    out = _truncate_telegram_html(text, 80)
    assert len(out) <= 80
    assert out.count("<b>") == out.count("</b>")
    assert "<b" not in out.replace("<b>", "").replace("</b>", "")


def test_truncation_closes_open_pre() -> None:
    from backend.telegram.sender import _truncate_telegram_html

    out = _truncate_telegram_html("<pre>" + ("z" * 500) + "</pre>", 100)
    assert out.count("<pre>") == out.count("</pre>") == 1


def test_truncation_is_noop_when_short() -> None:
    from backend.telegram.sender import _truncate_telegram_html

    assert _truncate_telegram_html("<b>hi</b>", 4096) == "<b>hi</b>"


def test_html_to_plain_strips_markup_and_entities() -> None:
    from backend.telegram.sender import _html_to_plain

    assert _html_to_plain("<b>a &amp; b</b> &lt;x&gt;") == "a & b <x>"


class _CapturingHTTP:

    def __init__(self, status_code: int = 200, text: str = "ok") -> None:
        self.status_code = status_code
        self.text = text
        self.posts: list[dict[str, Any]] = []

    async def post(self, url, json=None, **kwargs):
        self.posts.append(json or {})
        return self

    def json(self):
        return {"ret": 0}


async def test_send_message_truncates_on_a_tag_boundary() -> None:
    from backend.telegram.sender import _MAX_MESSAGE_LENGTH, TelegramSender

    sender = TelegramSender("tok", default_chat_id="1")
    http = _CapturingHTTP()
    sender._client = http

    md = "".join(f"**bold-{i:04d}** filler text here.\n" for i in range(400))
    assert await sender.send_message(md) is True

    text = http.posts[0]["text"]
    assert len(text) <= _MAX_MESSAGE_LENGTH
    assert text.count("<b>") == text.count("</b>"), "truncation split a <b> tag"
    assert not text.rstrip().endswith("<b")
    assert "<" not in text[text.rfind(">") + 1:]


async def test_send_message_plain_retry_strips_markup() -> None:
    from backend.telegram.sender import TelegramSender

    sender = TelegramSender("tok", default_chat_id="1")
    http = _CapturingHTTP(status_code=400, text="Bad Request: can't parse entities")
    sender._client = http

    await sender.send_message("**hi** <there> & more")
    assert len(http.posts) == 2
    retry = http.posts[1]
    assert retry["parse_mode"] == ""
    assert "<b>" not in retry["text"]
    assert "&amp;" not in retry["text"]
    assert retry["text"] == "hi <there> & more"


async def test_ios_send_message_reports_failure_without_agent() -> None:
    import backend.api.agent_state as agent_state
    from backend.ios_gateway import IOSGateway

    saved = dict(agent_state.active_agents)
    agent_state.active_agents.clear()
    try:
        gw = IOSGateway("ghost-user")
        assert await gw.send_message("nobody is listening", chat_id="ghost-user") is False
    finally:
        agent_state.active_agents.clear()
        agent_state.active_agents.update(saved)


async def test_ios_send_message_reports_success_with_agent() -> None:
    import backend.api.agent_state as agent_state
    from backend.ios_gateway import IOSGateway

    class _FakeAgent:
        def __init__(self):
            self.events = []

        async def _emit(self, ev):
            self.events.append(ev)

    saved = dict(agent_state.active_agents)
    agent_state.active_agents.clear()
    try:
        agent_state.active_agents["u1"] = {"agent": _FakeAgent()}
        gw = IOSGateway("u1")
        assert await gw.send_message("hello", chat_id="u1") is True
    finally:
        agent_state.active_agents.clear()
        agent_state.active_agents.update(saved)


async def test_apns_failure_does_not_suppress_the_next_banner() -> None:
    import backend.api.agent_state as agent_state
    from backend.ios_gateway import IOSGateway

    class _FakeAgent:
        async def _emit(self, ev):
            return None

    class _FlakyAPNs:
        enabled = True

        def __init__(self):
            self.calls = 0

        async def send(self, user_id, title, body, data=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("APNs down")
            return 1

    saved = dict(agent_state.active_agents)
    agent_state.active_agents.clear()
    try:
        agent_state.active_agents["u2"] = {"agent": _FakeAgent()}
        apns = _FlakyAPNs()
        gw = IOSGateway("u2", apns_sender=apns)
        await gw.send_message("first", chat_id="u2")
        await gw.send_message("second", chat_id="u2")
        assert apns.calls == 2
    finally:
        agent_state.active_agents.clear()
        agent_state.active_agents.update(saved)


class _FakeAPNsResult:
    def __init__(self, status: str, description: str, ok: bool = False) -> None:
        self.status = status
        self.description = description
        self.is_successful = ok


def _install_fake_aioapns(monkeypatch, results: list) -> list:
    import sys
    import types

    sent: list = []

    class _APNs:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def send_notification(self, req):
            sent.append(req.device_token)
            return results.pop(0) if results else _FakeAPNsResult("200", "", True)

    class _NotificationRequest:
        def __init__(self, device_token, message, push_type=None) -> None:
            self.device_token = device_token
            self.message = message

    mod = types.ModuleType("aioapns")
    mod.APNs = _APNs
    mod.NotificationRequest = _NotificationRequest
    mod.PushType = types.SimpleNamespace(ALERT="alert")
    monkeypatch.setitem(sys.modules, "aioapns", mod)
    return sent


def _apns_settings(tmp_path):
    import types

    key = tmp_path / "AuthKey_TEST.p8"
    key.write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    return types.SimpleNamespace(
        APNS_ENABLED=True,
        APNS_ENV="production",
        APNS_KEY_PATH=str(key),
        APNS_KEY_ID="KID",
        APNS_TEAM_ID="TID",
        APNS_BUNDLE_ID="com.example.hime",
    )


async def test_apns_revokes_bad_device_token(tmp_path, monkeypatch) -> None:
    from backend.config import settings
    from backend.ios_gateway import device_store
    from backend.ios_gateway.apns import APNSSender

    monkeypatch.setattr(settings, "MEMORY_DB_PATH", tmp_path)
    device_store.upsert_device_token("u", "tok-bad", "bid", "production")
    device_store.upsert_device_token("u", "tok-good", "bid", "production")

    sent = _install_fake_aioapns(
        monkeypatch,
        [
            _FakeAPNsResult("400", "BadDeviceToken"),
            _FakeAPNsResult("200", "", ok=True),
        ],
    )

    n = await APNSSender(_apns_settings(tmp_path)).send("u", "t", "b")
    assert n == 1
    assert sorted(sent) == ["tok-bad", "tok-good"]

    remaining = [t["device_token"] for t in device_store.list_device_tokens("u")]
    assert remaining == ["tok-good"], "BadDeviceToken must be revoked, not retried"


async def test_apns_still_revokes_unregistered(tmp_path, monkeypatch) -> None:
    from backend.config import settings
    from backend.ios_gateway import device_store
    from backend.ios_gateway.apns import APNSSender

    monkeypatch.setattr(settings, "MEMORY_DB_PATH", tmp_path)
    device_store.upsert_device_token("u", "tok-gone", "bid", "production")
    _install_fake_aioapns(monkeypatch, [_FakeAPNsResult("410", "Unregistered")])

    assert await APNSSender(_apns_settings(tmp_path)).send("u", "t", "b") == 0
    assert device_store.list_device_tokens("u") == []


async def test_apns_pushes_every_token_via_its_own_environment(
    tmp_path, monkeypatch,
) -> None:
    from backend.config import settings
    from backend.ios_gateway import device_store
    from backend.ios_gateway.apns import APNSSender

    monkeypatch.setattr(settings, "MEMORY_DB_PATH", tmp_path)
    device_store.upsert_device_token("u", "tok-prod", "bid", "production")
    device_store.upsert_device_token("u", "tok-sbox", "bid", "sandbox")

    sent = _install_fake_aioapns(monkeypatch, [])
    await APNSSender(_apns_settings(tmp_path)).send("u", "t", "b")
    assert sorted(sent) == ["tok-prod", "tok-sbox"]


def test_list_device_tokens_filters_by_environment(tmp_path, monkeypatch) -> None:
    from backend.config import settings
    from backend.ios_gateway import device_store

    monkeypatch.setattr(settings, "MEMORY_DB_PATH", tmp_path)
    device_store.upsert_device_token("u", "tok-prod", "bid", "production")
    device_store.upsert_device_token("u", "tok-sbox", "bid", "sandbox")

    assert len(device_store.list_device_tokens("u")) == 2
    prod = device_store.list_device_tokens("u", "production")
    assert [t["device_token"] for t in prod] == ["tok-prod"]
    sandbox = device_store.list_device_tokens("u", "sandbox")
    assert [t["device_token"] for t in sandbox] == ["tok-sbox"]


def test_non_additive_metrics_are_not_summed() -> None:
    from backend.data_readers.apple_health_features import (
        AGG_MEAN,
        FEATURE_SPEC,
        get_aggregate_sum_features,
    )

    for feature in (
        "environmental_audio",
        "headphone_audio",
        "physical_effort",
        "six_minute_walk",
    ):
        assert FEATURE_SPEC[feature]["aggregation"] == AGG_MEAN, feature
        assert feature not in get_aggregate_sum_features()

    assert "steps" in get_aggregate_sum_features()
    assert "active_energy" in get_aggregate_sum_features()


def _make_watch_db(path) -> None:
    now = time.time()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE health_samples_eav "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, f TEXT, ts REAL, v REAL)"
        )
        conn.executemany(
            "INSERT INTO health_samples_eav (f, ts, v) VALUES (?, ?, ?)",
            [("heart_rate", now - 60 * i, 60.0 + i) for i in range(1, 11)],
        )
        conn.commit()


@pytest.fixture
def forced_tz(request, monkeypatch):
    original = os.environ.get("TZ")
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    try:
        yield request.param
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


@pytest.mark.parametrize(
    "forced_tz", ["Asia/Shanghai", "America/New_York", "UTC"], indirect=True,
)
def test_watch_reader_default_window_is_in_the_past(tmp_path, monkeypatch, forced_tz):
    from backend.data_readers import watch_db_reader as wmod

    _make_watch_db(tmp_path / "watch.db")

    seen: list[float] = []
    real_read_sql = wmod.pd.read_sql_query

    def _spy(query, con, params=None):
        if params:
            seen.append(float(params[-1]))
        return real_read_sql(query, con, params=params)

    monkeypatch.setattr(wmod.pd, "read_sql_query", _spy)

    reader = wmod.WatchDBReader(tmp_path)
    df = reader.load_feature_data(["LiveUser"], "heart_rate", minutes=30)

    assert seen, "load_feature_data did not run its query"
    cutoff = seen[-1]
    now = time.time()
    assert cutoff < now, f"cutoff {cutoff} is in the future in TZ={forced_tz}"
    assert now - cutoff == pytest.approx(30 * 60, abs=10)
    assert not df.empty
    assert len(df) == 10


def test_watch_reader_explicit_since_ts_is_respected(tmp_path) -> None:
    from backend.data_readers.watch_db_reader import WatchDBReader

    _make_watch_db(tmp_path / "watch.db")
    reader = WatchDBReader(tmp_path)
    df = reader.load_feature_data(
        ["LiveUser"], "heart_rate", since_ts=time.time() - 5 * 60,
    )
    assert len(df) == 4


def test_watch_reader_date_range_is_tz_aware(tmp_path) -> None:
    from backend.data_readers.watch_db_reader import WatchDBReader

    _make_watch_db(tmp_path / "watch.db")
    populated = WatchDBReader(tmp_path).get_date_range(["LiveUser"])

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    fallback = WatchDBReader(empty_dir).get_date_range(["LiveUser"])

    for lo, hi in (populated, fallback):
        assert lo.tzinfo is not None and hi.tzinfo is not None
    assert isinstance(populated[1] < fallback[1], (bool, pd._libs.missing.NAType))


def test_data_store_date_range_is_tz_aware_and_comparable(tmp_path) -> None:
    from backend.data_readers.data_store_reader import DataStoreReader

    db = tmp_path / "LiveUser_data.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE samples (timestamp TEXT, feature_type TEXT, value REAL)"
        )
        conn.executemany(
            "INSERT INTO samples VALUES (?, ?, ?)",
            [
                ("2026-01-01T00:00:00", "heart_rate", 60.0),
                ("2026-01-02T00:00:00", "heart_rate", 61.0),
            ],
        )
        conn.commit()

    reader = DataStoreReader(tmp_path)
    lo, hi = reader.get_date_range(["LiveUser"])
    assert lo.tzinfo is not None and hi.tzinfo is not None
    df = reader.load_feature_data(["LiveUser"], "heart_rate", minutes=60 * 24 * 365 * 100)
    if not df.empty:
        assert bool((df["date"] >= lo).all())


def test_data_store_date_range_fallbacks_are_tz_aware(tmp_path) -> None:
    from backend.data_readers.data_store_reader import DataStoreReader

    reader = DataStoreReader(tmp_path)
    ranges = [
        reader.get_date_range([]),
        reader.get_date_range(["Ghost"]),
    ]
    for lo, hi in ranges:
        assert lo.tzinfo is not None and hi.tzinfo is not None
    now = pd.Timestamp.now(tz="UTC")
    for lo, hi in ranges:
        assert lo <= now and hi <= now + pd.Timedelta(seconds=5)


def test_data_store_reader_does_not_create_phantom_db(tmp_path) -> None:
    from backend.data_readers.data_store_reader import DataStoreReader

    reader = DataStoreReader(tmp_path)
    assert reader.load_feature_data(["Ghost"], "heart_rate").empty
    reader.get_date_range(["Ghost"])
    assert not (tmp_path / "Ghost_data.db").exists()
    assert "Ghost" not in reader.get_available_users()


def test_weixin_truncates_on_utf8_bytes() -> None:
    from backend.weixin.sender import _MAX_TEXT_BYTES, _truncate_utf8

    long_chinese = "健康" * 3000
    out = _truncate_utf8(long_chinese, _MAX_TEXT_BYTES)
    assert len(out.encode("utf-8")) <= _MAX_TEXT_BYTES
    assert out.endswith("…(truncated)")
    out.encode("utf-8").decode("utf-8")


def test_weixin_short_text_untouched() -> None:
    from backend.weixin.sender import _MAX_TEXT_BYTES, _truncate_utf8

    assert _truncate_utf8("hello", _MAX_TEXT_BYTES) == "hello"


async def test_weixin_send_message_caps_utf8_bytes_on_the_wire() -> None:
    from backend.weixin.sender import _MAX_TEXT_BYTES, WeixinSender

    sender = WeixinSender("bot-token", default_user_id="u1")
    http = _CapturingHTTP()
    sender._client = http

    await sender.send_message("健康" * 3000, context_token="ctx")

    assert http.posts, "no request was sent"
    body = http.posts[0]["msg"]["item_list"][0]["text_item"]["text"]
    assert len(body.encode("utf-8")) <= _MAX_TEXT_BYTES
    assert body.endswith("…(truncated)")


async def test_cdn_client_error_is_not_retried() -> None:
    from backend.weixin import cdn

    attempts = 0

    class _Resp:
        status_code = 403
        text = "nope"
        headers: dict[str, str] = {}

    class _Client:
        async def post(self, *a, **kw):
            nonlocal attempts
            attempts += 1
            return _Resp()

    with pytest.raises(cdn.CDNClientError):
        await cdn.upload_ciphertext(
            _Client(), "https://cdn.example/upload", None, "fk", b"x",
        )
    assert attempts == 1


async def test_cdn_server_error_is_retried_with_backoff(monkeypatch) -> None:
    from backend.weixin import cdn

    monkeypatch.setattr(cdn, "_UPLOAD_RETRY_BASE_DELAY", 0.001)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _record(delay):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(cdn.asyncio, "sleep", _record)

    attempts = 0

    class _Resp:
        status_code = 503
        text = "busy"
        headers: dict[str, str] = {}

    class _Client:
        async def post(self, *a, **kw):
            nonlocal attempts
            attempts += 1
            return _Resp()

    with pytest.raises(cdn.CDNServerError):
        await cdn.upload_ciphertext(
            _Client(), "https://cdn.example/upload", None, "fk", b"x",
        )
    assert attempts == cdn._UPLOAD_MAX_RETRIES
    assert len(sleeps) == cdn._UPLOAD_MAX_RETRIES - 1
    assert all(d > 0 for d in sleeps)


def _tg_update(update_id: int, chat_id: str = "42", text: str = "hi") -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "text": text,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "T"},
        },
    }


class _FakeTelegramClient:

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self._updates = updates

    async def get(self, url, params=None):
        batch, self._updates = self._updates, []

        class _Resp:
            status_code = 200

            def json(self):
                return {"ok": True, "result": batch}

        return _Resp()


def test_poller_state_path_defaults_never_touch_the_repo(monkeypatch) -> None:
    from backend.telegram.poller import _resolve_state_path as tg_resolve
    from backend.weixin.transport import _resolve_state_path as wx_resolve

    assert os.environ.get("PYTEST_CURRENT_TEST")
    assert tg_resolve(None) is None
    assert wx_resolve(None) is None

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from backend.config import settings

    assert tg_resolve(None) == Path(settings.MEMORY_DB_PATH) / "telegram_poller_state.json"
    assert wx_resolve(None) == Path(settings.MEMORY_DB_PATH) / "weixin_poller_state.json"


async def test_telegram_offset_survives_restart(tmp_path) -> None:
    from backend.telegram.poller import TelegramPoller

    state = tmp_path / "tg.json"
    seen: list[str] = []

    async def _record(env):
        seen.append(env.content)

    poller = TelegramPoller(
        "tok", _record, allowed_chat_ids=None, state_path=state,
    )
    poller._client = _FakeTelegramClient([_tg_update(1001)])
    for upd in await poller._fetch_updates():
        await poller._process_update(upd)

    assert poller._offset == 1002
    assert seen == ["hi"]
    assert state.exists()

    del poller
    revived = TelegramPoller(
        "tok", _record, allowed_chat_ids=None, state_path=state,
    )
    assert revived._offset == 1002, "restart lost the getUpdates offset"

    await revived._process_update(_tg_update(1001))
    assert seen == ["hi"], "restart re-answered an already-handled update"


async def test_telegram_corrupt_state_file_degrades_gracefully(tmp_path, caplog) -> None:
    from backend.telegram.poller import TelegramPoller

    state = tmp_path / "tg.json"
    state.write_text('{"offset": 99, "seen_i', encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        poller = TelegramPoller("tok", _noop_async, state_path=state)

    assert poller._offset == 0
    assert poller._seen_ids == {}
    assert any("state file" in r.getMessage() for r in caplog.records)

    poller._client = _FakeTelegramClient([_tg_update(7)])
    await poller._fetch_updates()
    assert json.loads(state.read_text(encoding="utf-8"))["offset"] == 8


async def test_telegram_state_file_is_not_a_dict(tmp_path) -> None:
    from backend.telegram.poller import TelegramPoller

    state = tmp_path / "tg.json"
    state.write_text("[1, 2, 3]", encoding="utf-8")
    assert TelegramPoller("tok", _noop_async, state_path=state)._offset == 0


async def test_telegram_state_from_another_bot_is_ignored(tmp_path) -> None:
    from backend.telegram.poller import TelegramPoller

    state = tmp_path / "tg.json"
    poller = TelegramPoller("tok-a", _noop_async, state_path=state)
    poller._client = _FakeTelegramClient([_tg_update(500)])
    await poller._fetch_updates()
    assert poller._offset == 501

    assert TelegramPoller("tok-b", _noop_async, state_path=state)._offset == 0


async def test_telegram_persisted_seen_ids_stay_bounded(tmp_path) -> None:
    from backend.telegram.poller import TelegramPoller

    state = tmp_path / "tg.json"

    seeder = TelegramPoller("tok", _noop_async, state_path=state)
    seeder._MAX_SEEN = 5_000
    seeder._seen_ids = {f"42:{i}": None for i in range(5_000)}
    seeder._save_state()
    assert len(json.loads(state.read_text(encoding="utf-8"))["seen_ids"]) == 5_000

    revived = TelegramPoller("tok", _noop_async, state_path=state)
    assert len(revived._seen_ids) <= revived._MAX_SEEN
    assert "42:4999" in revived._seen_ids
    assert "42:0" not in revived._seen_ids

    revived._MAX_SEEN = 100
    await revived._process_update(_tg_update(9_999))
    assert len(json.loads(state.read_text(encoding="utf-8"))["seen_ids"]) <= 100

    assert list(tmp_path.glob("*.tmp")) == []


async def test_telegram_unwritable_state_path_does_not_break_polling(tmp_path) -> None:
    from backend.telegram.poller import TelegramPoller

    blocked = tmp_path / "blocked.json"
    blocked.mkdir()

    poller = TelegramPoller("tok", _noop_async, state_path=blocked)
    poller._client = _FakeTelegramClient([_tg_update(3)])
    assert await poller._fetch_updates()
    assert poller._offset == 4
    assert poller._save_warned


def _wx_update(msg_id: str, user: str = "u1@im.wechat") -> dict[str, Any]:
    return {
        "message_id": msg_id,
        "from_user_id": user,
        "context_token": "ctx",
        "item_list": [{"text_item": {"text": "hello"}}],
    }


class _FakeWeixinClient:

    def __init__(self, cursor: str, msgs: list[dict[str, Any]]) -> None:
        self._payload = {"ret": 0, "get_updates_buf": cursor, "msgs": msgs}

    async def post(self, url, json=None, headers=None):
        payload, self._payload = self._payload, {"ret": 0, "get_updates_buf": "", "msgs": []}

        class _Resp:
            status_code = 200
            text = "ok"

            def json(self):
                return payload

        return _Resp()


async def test_weixin_cursor_survives_restart(tmp_path) -> None:
    from backend.weixin.transport import WeixinPoller

    state = tmp_path / "wx.json"
    seen: list[str] = []

    async def _record(env):
        seen.append(env.content)

    poller = WeixinPoller("bot", _record, None, state_path=state)
    poller._client = _FakeWeixinClient("buf-42", [_wx_update("m1")])
    for upd in await poller._fetch_updates():
        await poller._process_update(upd)

    assert poller._cursor == "buf-42"
    assert seen == ["hello"]

    del poller
    revived = WeixinPoller("bot", _record, None, state_path=state)
    assert revived._cursor == "buf-42", "restart lost the iLink getupdates cursor"

    await revived._process_update(_wx_update("m1"))
    assert seen == ["hello"], "restart re-answered an already-handled message"

    sent: dict[str, Any] = {}

    class _Capture:
        async def post(self, url, json=None, headers=None):
            sent.update(json or {})

            class _Resp:
                status_code = 200
                text = "ok"

                def json(self):
                    return {"ret": 0, "msgs": []}

            return _Resp()

    revived._client = _Capture()
    await revived._fetch_updates()
    assert sent["get_updates_buf"] == "buf-42"


async def test_weixin_corrupt_state_file_degrades_gracefully(tmp_path, caplog) -> None:
    from backend.weixin.transport import WeixinPoller

    state = tmp_path / "wx.json"
    state.write_text("not json at all", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        poller = WeixinPoller("bot", _noop_async, None, state_path=state)

    assert poller._cursor == ""
    assert poller._seen_ids == {}

    poller._client = _FakeWeixinClient("buf-1", [])
    await poller._fetch_updates()
    assert json.loads(state.read_text(encoding="utf-8"))["cursor"] == "buf-1"


async def test_weixin_state_from_another_bot_token_is_ignored(tmp_path) -> None:
    from backend.weixin.transport import WeixinPoller

    state = tmp_path / "wx.json"
    poller = WeixinPoller("bot-a", _noop_async, None, state_path=state)
    poller._client = _FakeWeixinClient("buf-a", [])
    await poller._fetch_updates()

    assert WeixinPoller("bot-b", _noop_async, None, state_path=state)._cursor == ""


async def test_weixin_persisted_seen_ids_stay_bounded(tmp_path) -> None:
    from backend.weixin.transport import WeixinPoller

    state = tmp_path / "wx.json"

    seeder = WeixinPoller("bot", _noop_async, None, state_path=state)
    seeder._MAX_SEEN = 5_000
    seeder._seen_ids = {f"m{i}": None for i in range(5_000)}
    seeder._save_state()
    assert len(json.loads(state.read_text(encoding="utf-8"))["seen_ids"]) == 5_000

    revived = WeixinPoller("bot", _noop_async, None, state_path=state)
    assert len(revived._seen_ids) <= revived._MAX_SEEN
    assert "m4999" in revived._seen_ids
    assert "m0" not in revived._seen_ids

    revived._MAX_SEEN = 100
    await revived._process_update(_wx_update("m9999"))
    assert len(json.loads(state.read_text(encoding="utf-8"))["seen_ids"]) <= 100

    assert list(tmp_path.glob("*.tmp")) == []
