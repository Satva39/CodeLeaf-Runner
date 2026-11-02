from __future__ import annotations

import hmac
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from .config import Settings
from .database_models import CreateDatabaseJobRequest
from .sql_validation import validate_database_sql

DATABASE_STREAM = "codeleaf:runner:database-jobs"
DATABASE_GROUP = "codeleaf-runner-database-workers"
DATABASE_WORKER_HEARTBEAT_KEY = "codeleaf:runner:database-worker:heartbeat"

CREATE_DATABASE_JOB_LUA = r"""
local active_key = KEYS[1]
local rate_key = KEYS[2]
local stream_key = KEYS[3]
local job_key = KEYS[4]
local job_id = ARGV[1]
local owner_id = ARGV[2]
local engine = ARGV[3]
local payload_json = ARGV[4]
local now = ARGV[5]
local ttl = tonumber(ARGV[6])
local active_limit = tonumber(ARGV[7])
local rate_limit = tonumber(ARGV[8])
local queue_limit = tonumber(ARGV[9])

local ids = redis.call('SMEMBERS', active_key)
for _, existing_id in ipairs(ids) do
  local existing_status = redis.call('HGET', 'codeleaf:runner:database-job:' .. existing_id, 'status')
  if (not existing_status) or (existing_status ~= 'queued' and existing_status ~= 'running') then
    redis.call('SREM', active_key, existing_id)
  end
end
if redis.call('SCARD', active_key) >= active_limit then return {'active_limit'} end
if redis.call('XLEN', stream_key) >= queue_limit then return {'queue_full'} end
local recent = redis.call('INCR', rate_key)
if recent == 1 then redis.call('EXPIRE', rate_key, 60) end
if recent > rate_limit then
  redis.call('DECR', rate_key)
  return {'rate_limit'}
end
redis.call('HSET', job_key,
  'job_id', job_id, 'owner_id', owner_id, 'engine', engine,
  'status', 'queued', 'created_at', now, 'updated_at', now,
  'payload_json', payload_json, 'result_json', '{}', 'attempts', '0')
redis.call('EXPIRE', job_key, ttl)
redis.call('SADD', active_key, job_id)
local stream_id = redis.call('XADD', stream_key, '*', 'job_id', job_id)
return {'ok', stream_id}
"""

CLAIM_DATABASE_JOB_LUA = r"""
local job_key = KEYS[1]
local lease_key = KEYS[2]
local now = ARGV[1]
local worker_id = ARGV[2]
local lease_seconds = tonumber(ARGV[3])
local status = redis.call('HGET', job_key, 'status')
if not status then return {'missing', '', ''} end
if status ~= 'queued' and status ~= 'running' then return {status, '', ''} end
if redis.call('GET', lease_key) then return {'busy', '', ''} end
local acquired = redis.call('SET', lease_key, worker_id, 'NX', 'EX', lease_seconds)
if not acquired then return {'busy', '', ''} end
local payload = redis.call('HGET', job_key, 'payload_json') or '{}'
local owner_id = redis.call('HGET', job_key, 'owner_id') or ''
redis.call('HSET', job_key, 'status', 'running', 'updated_at', now, 'worker_id', worker_id)
redis.call('HINCRBY', job_key, 'attempts', 1)
return {'running', payload, owner_id}
"""

CANCEL_DATABASE_JOB_LUA = r"""
local job_key = KEYS[1]
local active_key = KEYS[2]
local job_id = ARGV[1]
local now = ARGV[2]
local status = redis.call('HGET', job_key, 'status')
if not status then return {'missing'} end
if status == 'queued' then
  redis.call('HSET', job_key, 'status', 'cancelled', 'updated_at', now,
    'result_json', '{"error":"Database job was cancelled before execution."}')
  redis.call('SREM', active_key, job_id)
  return {'cancelled'}
end
if status == 'running' then return {'running'} end
return {status}
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_database_payload(request: CreateDatabaseJobRequest, settings: Settings) -> dict[str, Any]:
    if request.engine.value not in {"sqlite", "mysql", "postgresql"}:
        raise HTTPException(status_code=422, detail="Unsupported database engine.")
    setup_statements, query = validate_database_sql(request.setup_sql, request.query)
    if len(request.setup_sql.encode("utf-8")) > settings.database_max_setup_bytes:
        raise HTTPException(status_code=413, detail="Setup SQL exceeds the configured size limit.")
    if len(query.encode("utf-8")) > settings.database_max_query_bytes:
        raise HTTPException(status_code=413, detail="SQL query exceeds the configured size limit.")
    return {
        "engine": request.engine.value,
        "setup_statements": setup_statements,
        "query": query,
    }


async def enqueue_database_job(
    redis: Any, *, owner_id: str, payload: dict[str, Any], settings: Settings
) -> dict[str, str]:
    job_id = str(uuid4())
    now = _now()
    result = await redis.eval(
        CREATE_DATABASE_JOB_LUA,
        4,
        f"codeleaf:runner:database-owner-active:{owner_id}",
        f"codeleaf:runner:database-owner-rate:{owner_id}",
        DATABASE_STREAM,
        f"codeleaf:runner:database-job:{job_id}",
        job_id,
        owner_id,
        payload["engine"],
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        now,
        settings.database_job_ttl_seconds,
        settings.database_max_active_per_user,
        settings.database_jobs_per_minute,
        settings.database_max_queue_depth,
    )
    outcome = result[0]
    if outcome == "active_limit":
        raise HTTPException(status_code=429, detail="You already have the maximum number of active database jobs.")
    if outcome == "rate_limit":
        raise HTTPException(status_code=429, detail="Database execution rate limit reached. Try again shortly.")
    if outcome == "queue_full":
        raise HTTPException(status_code=503, detail="The database execution queue is full. Please retry shortly.")
    if outcome != "ok":
        raise HTTPException(status_code=503, detail="The database job could not be queued.")
    return {"job_id": job_id, "status": "queued", "created_at": now}


async def get_public_database_job(redis: Any, job_id: str, owner_id: str) -> dict[str, Any] | None:
    record = await redis.hgetall(f"codeleaf:runner:database-job:{job_id}")
    if not record or not hmac.compare_digest(record.get("owner_id", ""), owner_id):
        return None
    try:
        result = json.loads(record.get("result_json", "{}"))
    except (TypeError, json.JSONDecodeError):
        result = {}
    return {
        "job_id": record.get("job_id", job_id),
        "engine": record.get("engine", ""),
        "status": record.get("status", "failed"),
        "created_at": record.get("created_at", ""),
        "updated_at": record.get("updated_at", ""),
        "result": result if isinstance(result, dict) else {},
    }


async def cancel_database_job(redis: Any, job_id: str, owner_id: str) -> str | None:
    job_key = f"codeleaf:runner:database-job:{job_id}"
    record = await redis.hgetall(job_key)
    if not record or not hmac.compare_digest(record.get("owner_id", ""), owner_id):
        return None
    result = await redis.eval(
        CANCEL_DATABASE_JOB_LUA,
        2,
        job_key,
        f"codeleaf:runner:database-owner-active:{owner_id}",
        job_id,
        _now(),
    )
    return str(result[0])


async def claim_database_job(
    redis: Any, job_id: str, *, worker_id: str = "database-worker", lease_seconds: int = 90
) -> dict[str, Any] | None:
    result = await redis.eval(
        CLAIM_DATABASE_JOB_LUA, 2,
        f"codeleaf:runner:database-job:{job_id}",
        f"codeleaf:runner:database-job-lease:{job_id}",
        _now(), worker_id, lease_seconds,
    )
    if not result:
        return None
    if result[0] == "busy":
        return {"status": "busy"}
    if result[0] != "running":
        return None
    try:
        payload = json.loads(result[1] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return {"payload": payload if isinstance(payload, dict) else {}, "owner_id": str(result[2] or "")}


async def finish_database_job(
    redis: Any, job_id: str, *, owner_id: str, status: str,
    result: dict[str, Any], settings: Settings,
) -> None:
    job_key = f"codeleaf:runner:database-job:{job_id}"
    pipe = redis.pipeline(transaction=True)
    pipe.hset(job_key, mapping={
        "status": status,
        "updated_at": _now(),
        "result_json": json.dumps(result, separators=(",", ":"), ensure_ascii=False),
        "payload_json": "{}",
    })
    pipe.srem(f"codeleaf:runner:database-owner-active:{owner_id}", job_id)
    pipe.delete(f"codeleaf:runner:database-job-lease:{job_id}")
    pipe.expire(job_key, settings.database_job_ttl_seconds)
    await pipe.execute()
