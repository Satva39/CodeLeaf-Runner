from __future__ import annotations

import hmac
import json
from typing import Any

from .jobs import now_iso

RESERVE_SESSION_LUA = r"""
local owner_key = KEYS[1]
local session_key = KEYS[2]
local session_id = ARGV[1]
local owner_id = ARGV[2]
local metadata = ARGV[3]
local ttl = tonumber(ARGV[4])
local max_active = tonumber(ARGV[5])

local existing = redis.call('SMEMBERS', owner_key)
for _, existing_id in ipairs(existing) do
  if redis.call('EXISTS', 'codeleaf:runner:session:' .. existing_id) == 0 then
    redis.call('SREM', owner_key, existing_id)
  end
end

if redis.call('SCARD', owner_key) >= max_active then
  return {'active_limit'}
end

redis.call('SET', session_key, metadata, 'EX', ttl)
redis.call('SADD', owner_key, session_id)
redis.call('EXPIRE', owner_key, ttl)
return {'ok'}
"""

RELEASE_SESSION_LUA = r"""
local owner_key = KEYS[1]
local session_key = KEYS[2]
local session_id = ARGV[1]
redis.call('SREM', owner_key, session_id)
redis.call('DEL', session_key)
return {'ok'}
"""


def session_key(session_id: str) -> str:
    return f"codeleaf:runner:session:{session_id}"


def session_channel(session_id: str) -> str:
    return f"codeleaf:runner:session-control:{session_id}"


async def reserve_session(
    redis: Any,
    *,
    session_id: str,
    owner_id: str,
    ttl_seconds: int,
    max_active: int,
) -> bool:
    metadata = json.dumps(
        {"session_id": session_id, "owner_id": owner_id, "created_at": now_iso(), "status": "running"},
        separators=(",", ":"),
    )
    result = await redis.eval(
        RESERVE_SESSION_LUA,
        2,
        f"codeleaf:runner:owner-sessions:{owner_id}",
        session_key(session_id),
        session_id,
        owner_id,
        metadata,
        ttl_seconds,
        max_active,
    )
    return bool(result and result[0] == "ok")


async def release_session(redis: Any, *, session_id: str, owner_id: str) -> None:
    await redis.eval(
        RELEASE_SESSION_LUA,
        2,
        f"codeleaf:runner:owner-sessions:{owner_id}",
        session_key(session_id),
        session_id,
    )


async def request_session_termination(
    redis: Any, *, session_id: str, owner_id: str
) -> bool:
    raw = await redis.get(session_key(session_id))
    if not raw:
        return False
    try:
        metadata = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    recorded_owner = str(metadata.get("owner_id", "")) if isinstance(metadata, dict) else ""
    if not hmac.compare_digest(recorded_owner, owner_id):
        return False
    await redis.publish(session_channel(session_id), json.dumps({"type": "terminate"}))
    return True
