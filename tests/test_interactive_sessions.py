from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi.testclient import TestClient

from app import auth, interactive, main
from app.config import Settings
from app.interactive import _validate_init_message, piston_websocket_url
from app.sessions import RESERVE_SESSION_LUA, RELEASE_SESSION_LUA

SECRET = "phase-three-test-secret-with-at-least-32-characters"


class FakePubSub:
    def __init__(self, redis: "FakeRedis"):
        self.redis = redis
        self.queues: list[asyncio.Queue] = []
        self.channels: list[str] = []

    async def subscribe(self, channel: str):
        self.channels.append(channel)
        queue = self.redis.channels[channel]
        self.queues.append(queue)

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        if not self.queues:
            await asyncio.sleep(min(timeout or 0.01, 0.01))
            return None
        try:
            return await asyncio.wait_for(self.queues[0].get(), timeout=timeout or 0.01)
        except asyncio.TimeoutError:
            return None

    async def aclose(self):
        return None


class FakeRedis:
    def __init__(self):
        self.records: dict[str, str] = {}
        self.owner_sets: dict[str, set[str]] = defaultdict(set)
        self.channels: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)

    async def eval(self, script, key_count, *args):
        from app.security import RATE_LIMIT_LUA
        if script == RATE_LIMIT_LUA:
            return [1, 60, 1]
        if script == RESERVE_SESSION_LUA:
            owner_key, record_key, session_id, owner_id, metadata, _ttl, max_active = args
            if len(self.owner_sets[owner_key]) >= int(max_active):
                return ["active_limit"]
            self.records[record_key] = metadata
            self.owner_sets[owner_key].add(session_id)
            return ["ok"]
        if script == RELEASE_SESSION_LUA:
            owner_key, record_key, session_id = args
            self.records.pop(record_key, None)
            self.owner_sets[owner_key].discard(session_id)
            return ["ok"]
        raise AssertionError("Unexpected Redis Lua script")

    async def get(self, key):
        return self.records.get(key)

    async def publish(self, channel, data):
        await self.channels[channel].put({"type": "message", "data": data})
        return 1

    def pubsub(self):
        return FakePubSub(self)


class FakePiston:
    async def list_runtimes(self):
        return [{"language": "python", "version": "3.12.0", "aliases": ["py"]}]


class FakeUpstream:
    def __init__(self):
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, raw: str):
        packet = json.loads(raw)
        self.sent.append(packet)
        if packet.get("type") == "init":
            await self.queue.put(json.dumps({"type": "runtime", "language": "python", "version": "3.12.0"}))
            await self.queue.put(json.dumps({"type": "stage", "stage": "run"}))
        elif packet.get("type") == "data" and packet.get("stream") == "stdin":
            await self.queue.put(json.dumps({"type": "data", "stream": "stdout", "data": f"received:{packet['data']}"}))
            await self.queue.put(json.dumps({"type": "exit", "stage": "run", "code": 0, "signal": None}))
        elif packet.get("type") == "signal":
            await self.queue.put(json.dumps({"type": "exit", "stage": "run", "code": None, "signal": packet.get("signal")}))

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        self.closed = True


def configured_app(monkeypatch):
    settings = Settings(service_token=SECRET)
    monkeypatch.setattr(interactive, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    redis = FakeRedis()
    main.app.state.redis = redis
    main.app.state.piston = FakePiston()
    upstream = FakeUpstream()
    monkeypatch.setattr(interactive, "connect_piston_websocket", lambda _settings: async_return(upstream))
    return TestClient(main.app), redis, upstream


async def async_return(value):
    return value


def auth_headers(owner="learner-1"):
    return {"X-Runner-Key": SECRET, "X-CodeLeaf-User": owner}


def test_piston_websocket_url_converts_http_schemes():
    assert piston_websocket_url("http://piston:2000/") == "ws://piston:2000/api/v2/connect"
    assert piston_websocket_url("https://runner.internal") == "wss://runner.internal/api/v2/connect"


def test_interactive_init_accepts_multi_file_project():
    settings = Settings(service_token=SECRET)
    entry, payload = _validate_init_message(
        {
            "type": "init",
            "language": "python",
            "files": [
                {"name": "main.py", "content": "from helper import message\nprint(message())"},
                {"name": "helper.py", "content": "def message(): return 'hello'"},
            ],
        },
        settings,
    )
    assert entry["key"] == "python"
    assert [item["name"] for item in payload["files"]] == ["main.py", "helper.py"]


def test_interactive_init_rejects_path_traversal_and_unknown_fields():
    settings = Settings(service_token=SECRET)
    for payload in (
        {"language": "python", "files": [{"name": "../main.py", "content": "print(1)"}]},
        {"language": "python", "source": "print(1)", "shell": "rm -rf"},
    ):
        try:
            _validate_init_message(payload, settings)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid session init was accepted")


def test_interactive_websocket_streams_stdin_and_stdout(monkeypatch):
    client, redis, upstream = configured_app(monkeypatch)
    with client.websocket_connect("/v1/sessions", headers=auth_headers()) as ws:
        ws.send_json({"type": "init", "language": "python", "source": "name = input(); print(name)"})
        session = ws.receive_json()
        assert session["type"] == "session"
        assert session["session_id"]
        runtime = ws.receive_json()
        assert runtime["type"] == "runtime"
        stage = ws.receive_json()
        assert stage == {"type": "stage", "stage": "run"}
        ws.send_json({"type": "data", "stream": "stdin", "data": "CodeLeaf"})
        output = ws.receive_json()
        assert output["data"] == "received:CodeLeaf"
        exit_event = ws.receive_json()
        assert exit_event["type"] == "exit"
        assert exit_event["code"] == 0
    assert upstream.sent[0]["type"] == "init"
    assert upstream.sent[-1]["type"] == "data"
    assert upstream.closed is True
    assert not redis.records


def test_interactive_websocket_rejects_missing_service_auth(monkeypatch):
    client, _redis, _upstream = configured_app(monkeypatch)
    try:
        with client.websocket_connect("/v1/sessions", headers={"X-CodeLeaf-User": "learner-1"}):
            raise AssertionError("Unauthenticated websocket was accepted")
    except Exception as exc:
        assert "4401" in str(exc) or getattr(exc, "code", None) == 4401


def test_interactive_session_termination_is_owner_scoped(monkeypatch):
    client, redis, _upstream = configured_app(monkeypatch)
    record_key = "codeleaf:runner:session:00000000-0000-4000-8000-000000000001"
    redis.records[record_key] = json.dumps({"session_id": "00000000-0000-4000-8000-000000000001", "owner_id": "learner-1"})
    response = client.delete(
        "/v1/sessions/00000000-0000-4000-8000-000000000001",
        headers=auth_headers("learner-2"),
    )
    assert response.status_code == 404

    response = client.delete(
        "/v1/sessions/00000000-0000-4000-8000-000000000001",
        headers=auth_headers("learner-1"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "termination_requested"


def test_interactive_session_quota_rejects_second_session(monkeypatch):
    client, redis, _upstream = configured_app(monkeypatch)
    from app.sessions import reserve_session

    async def run():
        assert await reserve_session(
            redis, session_id="first", owner_id="learner-1", ttl_seconds=180, max_active=1
        )
        assert not await reserve_session(
            redis, session_id="second", owner_id="learner-1", ttl_seconds=180, max_active=1
        )

    asyncio.run(run())


def test_interactive_websocket_forwards_process_termination_signal(monkeypatch):
    client, _redis, upstream = configured_app(monkeypatch)
    with client.websocket_connect("/v1/sessions", headers=auth_headers()) as ws:
        ws.send_json({"type": "init", "language": "python", "source": "while True: pass"})
        assert ws.receive_json()["type"] == "session"
        assert ws.receive_json()["type"] == "runtime"
        assert ws.receive_json()["type"] == "stage"
        ws.send_json({"type": "signal", "signal": "SIGKILL"})
        assert ws.receive_json()["type"] == "exit"
    assert any(packet == {"type": "signal", "signal": "SIGKILL"} for packet in upstream.sent)


def test_interactive_session_rejects_stdout_injection(monkeypatch):
    client, _redis, upstream = configured_app(monkeypatch)
    with client.websocket_connect("/v1/sessions", headers=auth_headers()) as ws:
        ws.send_json({"type": "init", "language": "python", "source": "input()"})
        assert ws.receive_json()["type"] == "session"
        assert ws.receive_json()["type"] == "runtime"
        assert ws.receive_json()["type"] == "stage"
        ws.send_json({"type": "data", "stream": "stdout", "data": "fake output"})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert "stdin" in error["message"]
    assert any(packet.get("type") == "signal" and packet.get("signal") == "SIGKILL" for packet in upstream.sent)


def test_rest_termination_signals_the_active_piston_session(monkeypatch):
    client, _redis, upstream = configured_app(monkeypatch)
    with client.websocket_connect("/v1/sessions", headers=auth_headers()) as ws:
        ws.send_json({"type": "init", "language": "python", "source": "while True: pass"})
        session = ws.receive_json()
        assert session["type"] == "session"
        assert ws.receive_json()["type"] == "runtime"
        assert ws.receive_json()["type"] == "stage"

        response = client.delete(
            f"/v1/sessions/{session['session_id']}",
            headers=auth_headers(),
        )
        assert response.status_code == 200
        control = ws.receive_json()
        assert control == {"type": "control", "status": "termination_requested"}
        exit_event = ws.receive_json()
        assert exit_event["type"] == "exit"
        assert exit_event["signal"] == "SIGKILL"

    assert any(packet == {"type": "signal", "signal": "SIGKILL"} for packet in upstream.sent)
