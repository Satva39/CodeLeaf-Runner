from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any
from uuid import uuid4

from .config import get_settings

RATE_LIMIT_LUA = r"""
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end
local ttl = redis.call('TTL', KEYS[1])
if count > tonumber(ARGV[2]) then return {0, ttl, count} end
return {1, ttl, count}
"""

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"),
]


def _client_ip(scope: dict[str, Any]) -> str:
    # Deliberately do not trust X-Forwarded-For. Configure a trusted proxy at the
    # deployment edge rather than accepting client-supplied forwarding headers.
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        value = str(client[0])
        return value[:64]
    return "unknown"


def _hash_client_ip(ip: str, secret: str) -> str:
    # Keyed hashing prevents simple reverse-enumeration of IPv4 addresses from Redis.
    key = secret.encode("utf-8")
    digest = hmac.new(key, (ip or "unknown").encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:24]


async def reserve_websocket_ip_budget(redis: Any, ip: str, settings) -> bool:
    """Rate-limit new interactive WebSocket sessions without trusting proxy headers."""
    key = f"codeleaf:runner:websocket-rate:{_hash_client_ip(ip, settings.token_value)}"
    result = await redis.eval(
        RATE_LIMIT_LUA,
        1,
        key,
        60,
        settings.websocket_connections_per_ip_per_minute,
    )
    return bool(result and int(result[0]) == 1)


async def _send_json(send, status_code: int, payload: dict[str, Any], request_id: str, extra_headers=None):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"x-request-id", request_id.encode("ascii", errors="ignore")),
        *_SECURITY_HEADERS,
        (b"cache-control", b"no-store"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RequestSecurityMiddleware:
    """Bound API input, enforce a Redis-backed IP request budget, and add headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        path = str(scope.get("path", ""))
        raw_headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied_id = raw_headers.get(b"x-request-id", b"").decode("latin1", errors="ignore")
        request_id = supplied_id if _REQUEST_ID_RE.fullmatch(supplied_id) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        # Apply the cheap per-IP budget before consuming the request body. This
        # prevents clients that already exceed their budget from making the API
        # buffer even the bounded maximum body on every rejected request.
        if path.startswith("/v1/"):
            request_app = scope.get("app")
            redis = getattr(getattr(request_app, "state", None), "redis", None)
            if redis is None:
                await _send_json(send, 503, {"detail": "Rate limiting service is unavailable.", "request_id": request_id}, request_id)
                return
            ip = _client_ip(scope)
            ip_hash = _hash_client_ip(ip, settings.token_value)
            key = f"codeleaf:runner:request-rate:{ip_hash}"
            try:
                result = await redis.eval(
                    RATE_LIMIT_LUA, 1, key,
                    settings.request_rate_window_seconds,
                    settings.request_rate_limit_per_ip,
                )
                if isinstance(result, (list, tuple)) and len(result) >= 2 and int(result[0]) == 0:
                    retry_after = max(1, int(result[1]))
                    await _send_json(
                        send, 429,
                        {"detail": "Request rate limit exceeded.", "request_id": request_id},
                        request_id,
                        [(b"retry-after", str(retry_after).encode("ascii"))],
                    )
                    return
            except Exception:
                await _send_json(send, 503, {"detail": "Request throttling is temporarily unavailable.", "request_id": request_id}, request_id)
                return

        content_length = raw_headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_api_body_bytes:
                    await _send_json(send, 413, {"detail": "Request body exceeds the configured limit.", "request_id": request_id}, request_id)
                    return
            except ValueError:
                await _send_json(send, 400, {"detail": "Invalid Content-Length header.", "request_id": request_id}, request_id)
                return

        # Buffer only up to the configured request limit, so chunked uploads cannot
        # bypass the Content-Length guard. API requests are intentionally small.
        buffered: list[dict[str, Any]] = []
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > settings.max_api_body_bytes:
                    await _send_json(send, 413, {"detail": "Request body exceeds the configured limit.", "request_id": request_id}, request_id)
                    return
                if not message.get("more_body", False):
                    break
            elif message.get("type") == "http.disconnect":
                return

        async def replay_receive():
            if buffered:
                return buffered.pop(0)
            return await receive()

        async def secure_send(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                names = {key.lower() for key, _ in headers}
                for key, value in _SECURITY_HEADERS:
                    if key not in names:
                        headers.append((key, value))
                if b"x-request-id" not in names:
                    headers.append((b"x-request-id", request_id.encode("ascii")))
                if path.startswith("/v1/") and b"cache-control" not in names:
                    headers.append((b"cache-control", b"no-store"))
                if scope.get("scheme") == "https" and b"strict-transport-security" not in names:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, replay_receive, secure_send)
