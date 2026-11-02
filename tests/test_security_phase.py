from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI

from app.auth import valid_any_service_key
from app.config import Settings
from app.security import RATE_LIMIT_LUA, RequestSecurityMiddleware, reserve_websocket_ip_budget

SECRET = "phase-five-unit-test-service-token-long-enough-0001"
PREVIOUS = "phase-five-previous-service-token-long-enough-0002"


class RateRedis:
    def __init__(self, result=None):
        self.result = result or [1, 60, 1]
        self.calls = 0

    async def eval(self, script, count, key, window, limit):
        assert script == RATE_LIMIT_LUA
        assert count == 1
        assert key.startswith(("codeleaf:runner:request-rate:", "codeleaf:runner:websocket-rate:"))
        assert window >= 10
        if key.startswith("codeleaf:runner:request-rate:"):
            assert limit >= 60
        else:
            assert limit >= 1
        self.calls += 1
        return self.result


def build_app(redis, max_body=1024):
    app = FastAPI()
    app.state.redis = redis

    @app.post("/v1/test")
    async def endpoint(payload: dict):
        return {"received": payload}

    @app.get("/health/live")
    async def health():
        return {"status": "alive"}

    app.add_middleware(RequestSecurityMiddleware)
    return app


def request(app, method, path, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


def test_service_key_rotation_accepts_primary_and_previous(monkeypatch):
    settings = Settings(service_token=SECRET, previous_service_token=PREVIOUS)
    assert valid_any_service_key(SECRET, settings)
    assert valid_any_service_key(PREVIOUS, settings)
    assert not valid_any_service_key("not-the-token", settings)


def test_security_middleware_adds_hardening_headers_and_request_id():
    app = build_app(RateRedis())
    response = request(
        app, "POST", "/v1/test", json={"ok": True},
        headers={"X-Request-ID": "request_12345678"},
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "request_12345678"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


def test_security_middleware_rejects_request_over_configured_limit(monkeypatch):
    from app import security
    monkeypatch.setattr(security, "get_settings", lambda: Settings(service_token=SECRET, max_api_body_bytes=65536))
    app = build_app(RateRedis())
    payload = "x" * 70000
    response = request(app, "POST", "/v1/test", content=payload)
    assert response.status_code == 413
    assert "request_id" in response.json()


def test_security_middleware_returns_429_when_ip_budget_is_exceeded():
    app = build_app(RateRedis([0, 17, 99]))
    response = request(app, "POST", "/v1/test", json={"ok": True})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "17"


def test_health_route_does_not_consume_api_request_budget():
    redis = RateRedis()
    app = build_app(redis)
    response = request(app, "GET", "/health/live")
    assert response.status_code == 200
    assert redis.calls == 0


def test_security_middleware_fails_closed_when_rate_store_is_unavailable():
    class BrokenRedis:
        async def eval(self, *_args):
            raise RuntimeError("redis unavailable")

    response = request(build_app(BrokenRedis()), "POST", "/v1/test", json={"ok": True})
    assert response.status_code == 503


def test_websocket_ip_budget_uses_separate_hashed_key():
    redis = RateRedis()
    settings = Settings(service_token=SECRET, websocket_connections_per_ip_per_minute=3)
    assert asyncio.run(reserve_websocket_ip_budget(redis, "192.0.2.10", settings))
    assert redis.calls == 1


def test_websocket_ip_budget_rejects_over_limit():
    redis = RateRedis([0, 12, 4])
    settings = Settings(service_token=SECRET, websocket_connections_per_ip_per_minute=3)
    assert not asyncio.run(reserve_websocket_ip_budget(redis, "192.0.2.10", settings))


def test_websocket_origins_reject_wildcards_and_paths():
    import pytest
    with pytest.raises(ValueError):
        Settings(service_token=SECRET, allowed_websocket_origins_csv="*")
    with pytest.raises(ValueError):
        Settings(service_token=SECRET, allowed_websocket_origins_csv="https://example.com/app")
    assert Settings(service_token=SECRET, allowed_websocket_origins_csv="https://example.com/").allowed_websocket_origins == {"https://example.com"}


def test_production_api_startup_does_not_receive_docker_gateway_secret():
    from app.main import validate_api_startup_settings
    settings = Settings(
        service_token=SECRET,
        production_mode=True,
        allowed_websocket_origins_csv="https://codeleaf.example",
        docker_gateway_token="",
    )
    # The API is intentionally not given the gateway credential. The gateway and
    # database worker validate it in their own isolated processes.
    validate_api_startup_settings(settings)


def test_production_api_startup_requires_https_origin():
    import pytest
    from app.main import validate_api_startup_settings
    settings = Settings(service_token=SECRET, production_mode=True)
    with pytest.raises(RuntimeError, match="WebSocket origins must use HTTPS"):
        validate_api_startup_settings(settings)


def test_client_ip_rate_limit_key_is_keyed_hash_not_raw_address():
    from app.security import _hash_client_ip
    first = _hash_client_ip("192.0.2.10", SECRET)
    second = _hash_client_ip("192.0.2.10", PREVIOUS)
    assert len(first) == 24
    assert first != second
    assert "192.0.2.10" not in first


def test_rate_limited_request_is_rejected_before_body_is_consumed():
    """A client over its IP budget must not make middleware buffer a body."""
    app = build_app(RateRedis([0, 19, 100]))
    middleware = RequestSecurityMiddleware(app)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/test",
        "raw_path": b"/v1/test",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("192.0.2.55", 12345),
        "server": ("test", 80),
        "app": app,
        "state": {},
    }
    receive_calls = 0
    sent = []

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b'{"payload":"large"}', "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 429
    assert receive_calls == 0
