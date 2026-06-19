"""Webhook lifecycle tests for the FastAPI dispatcher.

The Telnyx HTTP surface is stubbed so no network is touched; these tests cover
signature enforcement, connection filtering, inbound answer, and slot cleanup
when a call ends before it is answered.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import types

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

import offleash.server as server
from offleash.limits import CallLimiter
from offleash.settings import Settings, get_settings
from tests.conftest import FakeCall, until


class FakeTelnyxClient:
    def __init__(self, *_args, **_kwargs) -> None:
        self.calls: dict[str, FakeCall] = {}

    def call(self, call_control_id: str) -> FakeCall:
        existing = self.calls.get(call_control_id)
        if existing is None:
            existing = FakeCall()
            self.calls[call_control_id] = existing
        return existing

    async def aclose(self) -> None:
        pass


def _event(event_type: str, payload: dict) -> bytes:
    return json.dumps({"data": {"event_type": event_type, "payload": payload}}).encode()


def _headers(sk: SigningKey, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    sig = base64.b64encode(sk.sign(f"{ts}|".encode() + body).signature).decode()
    return {
        "telnyx-signature-ed25519": sig,
        "telnyx-timestamp": ts,
        "content-type": "application/json",
    }


@pytest.fixture
def client(monkeypatch):
    sk = SigningKey.generate()
    pub = base64.b64encode(bytes(sk.verify_key)).decode()
    monkeypatch.setenv("TELNYX_API_KEY", "k")
    monkeypatch.setenv("TELNYX_PUBLIC_KEY", pub)
    monkeypatch.setenv("TELNYX_CONNECTION_ID", "conn-1")
    monkeypatch.setattr(server, "TelnyxClient", FakeTelnyxClient)
    get_settings.cache_clear()
    with TestClient(server.app) as test_client:
        yield test_client, sk
    get_settings.cache_clear()


def test_invalid_signature_is_rejected(client) -> None:
    test_client, _sk = client
    body = _event("call.initiated", {"call_control_id": "cc-1"})
    resp = test_client.post(
        "/webhook",
        content=body,
        headers={"telnyx-signature-ed25519": "bad", "telnyx-timestamp": "0"},
    )
    assert resp.status_code == 403


def test_foreign_connection_is_ignored(client) -> None:
    test_client, sk = client
    body = _event(
        "call.initiated",
        {"call_control_id": "cc-2", "direction": "incoming", "connection_id": "other"},
    )
    resp = test_client.post("/webhook", content=body, headers=_headers(sk, body))
    assert resp.status_code == 200
    assert "cc-2" not in test_client.app.state.agents


def test_inbound_call_is_answered_and_registered(client) -> None:
    test_client, sk = client
    body = _event(
        "call.initiated",
        {
            "call_control_id": "cc-3",
            "direction": "incoming",
            "connection_id": "conn-1",
            "from": "+15551112222",
        },
    )
    resp = test_client.post("/webhook", content=body, headers=_headers(sk, body))
    assert resp.status_code == 200

    agents = test_client.app.state.agents
    assert "cc-3" in agents
    assert test_client.app.state.limiter.active == 1
    fake_call = test_client.app.state.telnyx.calls["cc-3"]
    assert ("answer",) in fake_call.actions


@pytest.fixture
def client_cap1(monkeypatch):
    sk = SigningKey.generate()
    pub = base64.b64encode(bytes(sk.verify_key)).decode()
    monkeypatch.setenv("TELNYX_API_KEY", "k")
    monkeypatch.setenv("TELNYX_PUBLIC_KEY", pub)
    monkeypatch.setenv("TELNYX_CONNECTION_ID", "conn-1")
    monkeypatch.setenv("MAX_CONCURRENT_CALLS", "1")
    monkeypatch.setattr(server, "TelnyxClient", FakeTelnyxClient)
    get_settings.cache_clear()
    with TestClient(server.app) as test_client:
        yield test_client, sk
    get_settings.cache_clear()


def _incoming(ccid: str, num: str) -> bytes:
    return _event(
        "call.initiated",
        {
            "call_control_id": ccid,
            "direction": "incoming",
            "connection_id": "conn-1",
            "from": num,
        },
    )


def test_capacity_rejects_second_call(client_cap1) -> None:
    test_client, sk = client_cap1
    first = _incoming("cc-a", "+15551110000")
    test_client.post("/webhook", content=first, headers=_headers(sk, first))
    assert test_client.app.state.limiter.active == 1

    second = _incoming("cc-b", "+15552220000")
    test_client.post("/webhook", content=second, headers=_headers(sk, second))

    # The over-capacity call is hung up, never registered, and holds no slot.
    assert "cc-b" not in test_client.app.state.agents
    assert test_client.app.state.limiter.active == 1
    assert test_client.app.state.telnyx.calls["cc-b"].hung_up


def _make_app() -> types.SimpleNamespace:
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    # Empty connection id disables the connection filter (and overrides any
    # value the developer's .env would otherwise supply).
    app.state.settings = Settings(
        telnyx_api_key="k", telnyx_public_key="p", telnyx_connection_id=""
    )
    app.state.agents = {}
    app.state.limiter = CallLimiter(10)
    app.state.telnyx = FakeTelnyxClient()
    return app


@pytest.mark.asyncio
async def test_answered_then_hangup_releases_slot() -> None:
    app = _make_app()
    payload = {"call_control_id": "cc", "direction": "incoming", "from": "+15551234567"}

    await server._ensure_registered(app, payload, "cc")
    assert "cc" in app.state.agents
    assert app.state.limiter.active == 1

    await server._dispatch(app, "call.answered", payload, "cc")
    agent = app.state.agents["cc"]
    assert agent.run_task is not None
    await asyncio.sleep(0.02)  # let the run loop start and greet

    await server._dispatch(app, "call.hangup", payload, "cc")
    await asyncio.wait_for(agent.run_task, timeout=2.0)

    # The run-task done callback fires _finish_call: slot released, deregistered.
    await until(lambda: "cc" not in app.state.agents)
    assert app.state.limiter.active == 0
    assert agent._closed


def test_hangup_before_answer_releases_slot(client) -> None:
    test_client, sk = client
    init = _event(
        "call.initiated",
        {
            "call_control_id": "cc-4",
            "direction": "incoming",
            "connection_id": "conn-1",
            "from": "+15551112222",
        },
    )
    test_client.post("/webhook", content=init, headers=_headers(sk, init))
    assert test_client.app.state.limiter.active == 1

    hangup = _event("call.hangup", {"call_control_id": "cc-4"})
    test_client.post("/webhook", content=hangup, headers=_headers(sk, hangup))

    assert "cc-4" not in test_client.app.state.agents
    assert test_client.app.state.limiter.active == 0
