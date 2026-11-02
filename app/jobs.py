from __future__ import annotations

import hmac
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from .config import Settings
from .models import CreateJobRequest
from .registry import resolve_language

STREAM_KEY = "codeleaf:runner:jobs"
CONSUMER_GROUP = "codeleaf-runner-workers"

CREATE_JOB_LUA = r"""
local active_key = KEYS[1]
local rate_key = KEYS[2]
local stream_key = KEYS[3]
local job_key = KEYS[4]
local job_id = ARGV[1]
local owner_id = ARGV[2]
local language = ARGV[3]
local payload_json = ARGV[4]
local now = ARGV[5]
local ttl = tonumber(ARGV[6])
local active_limit = tonumber(ARGV[7])
local rate_limit = tonumber(ARGV[8])
local queue_limit = tonumber(ARGV[9])

-- Prune completed/expired IDs so stale active-set entries cannot block a learner forever.
local ids = redis.call('SMEMBERS', active_key)
for _, existing_id in ipairs(ids) do
  local existing_status = redis.call('HGET', 'codeleaf:runner:job:' .. existing_id, 'status')
  if (not existing_status) or (existing_status ~= 'queued' and existing_status ~= 'running') then
    redis.call('SREM', active_key, existing_id)
  end
end

if redis.call('SCARD', active_key) >= active_limit then
  return {'active_limit'}
end

if redis.call('XLEN', stream_key) >= queue_limit then
  return {'queue_full'}
end

local recent = redis.call('INCR', rate_key)
if recent == 1 then redis.call('EXPIRE', rate_key, 60) end
if recent > rate_limit then
  redis.call('DECR', rate_key)
  return {'rate_limit'}
end

redis.call('HSET', job_key,
  'job_id', job_id,
  'owner_id', owner_id,
  'language', language,
  'status', 'queued',
  'created_at', now,
  'updated_at', now,
  'payload_json', payload_json,
  'result_json', '{}',
  'attempts', '0'
)
redis.call('EXPIRE', job_key, ttl)
redis.call('SADD', active_key, job_id)
local stream_id = redis.call('XADD', stream_key, '*', 'job_id', job_id)
return {'ok', stream_id}
"""

CANCEL_JOB_LUA = r"""
local job_key = KEYS[1]
local active_key = KEYS[2]
local job_id = ARGV[1]
local now = ARGV[2]
local status = redis.call('HGET', job_key, 'status')
if not status then return {'missing'} end
if status == 'queued' then
  redis.call('HSET', job_key,
    'status', 'cancelled',
    'updated_at', now,
    'result_json', '{"message":"Job was cancelled before execution."}'
  )
  redis.call('SREM', active_key, job_id)
  return {'cancelled'}
end
if status == 'running' then return {'running'} end
return {status}
"""

CLAIM_JOB_LUA = r"""
local job_key = KEYS[1]
local lease_key = KEYS[2]
local now = ARGV[1]
local worker_id = ARGV[2]
local lease_seconds = tonumber(ARGV[3])
local status = redis.call('HGET', job_key, 'status')
if not status then return {'missing', '', ''} end
if status ~= 'queued' and status ~= 'running' then return {status, '', ''} end
local current_lease = redis.call('GET', lease_key)
if current_lease then return {'busy', '', ''} end
local acquired = redis.call('SET', lease_key, worker_id, 'NX', 'EX', lease_seconds)
if not acquired then return {'busy', '', ''} end
local payload = redis.call('HGET', job_key, 'payload_json') or '{}'
local owner_id = redis.call('HGET', job_key, 'owner_id') or ''
redis.call('HSET', job_key, 'status', 'running', 'updated_at', now, 'worker_id', worker_id)
redis.call('HINCRBY', job_key, 'attempts', 1)
return {'running', payload, owner_id}
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def prepare_payload(
    request: CreateJobRequest,
    settings: Settings,
) -> tuple[dict[str, Any], dict[str, Any]]:
    entry = resolve_language(request.language)
    if request.source is not None:
        files = [{"name": f"main.{entry['extension']}", "content": request.source}]
    else:
        files = [{"name": item.name, "content": item.content} for item in request.files]

    if len(files) > settings.max_files_per_job:
        raise HTTPException(status_code=413, detail="This job contains too many files.")
    total_bytes = 0
    for item in files:
        current_bytes = len(item["content"].encode("utf-8"))
        if current_bytes > settings.max_file_bytes:
            raise HTTPException(status_code=413, detail=f"{item['name']} exceeds the per-file limit.")
        total_bytes += current_bytes
    if total_bytes > settings.max_total_source_bytes:
        raise HTTPException(status_code=413, detail="The combined source exceeds the job size limit.")
    if len(request.stdin.encode("utf-8")) > settings.max_stdin_bytes:
        raise HTTPException(status_code=413, detail="Standard input exceeds the job size limit.")
    if len(request.args) > settings.max_args or any(len(arg) > settings.max_arg_length for arg in request.args):
        raise HTTPException(status_code=413, detail="The execution arguments exceed the configured limits.")

    payload = {
        "language": entry["key"],
        "piston_language": entry["piston_language"],
        "version": request.version,
        "files": files,
        "stdin": request.stdin,
        "args": request.args,
    }
    return entry, payload


async def enqueue_job(
    redis: Any,
    *,
    owner_id: str,
    language: str,
    payload: dict[str, Any],
    settings: Settings,
) -> dict[str, str]:
    job_id = str(uuid4())
    now = now_iso()
    owner_active_key = f"codeleaf:runner:owner-active:{owner_id}"
    job_key = f"codeleaf:runner:job:{job_id}"
    result = await redis.eval(
        CREATE_JOB_LUA,
        4,
        owner_active_key,
        f"codeleaf:runner:owner-rate:{owner_id}",
        STREAM_KEY,
        job_key,
        job_id,
        owner_id,
        language,
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        now,
        settings.job_ttl_seconds,
        settings.max_active_per_user,
        settings.max_jobs_per_minute,
        settings.max_queue_depth,
    )
    outcome = result[0]
    if outcome == "active_limit":
        raise HTTPException(status_code=429, detail="You already have the maximum number of active code jobs.")
    if outcome == "rate_limit":
        raise HTTPException(status_code=429, detail="Code execution rate limit reached. Try again in a minute.")
    if outcome == "queue_full":
        raise HTTPException(status_code=503, detail="The execution queue is currently full. Please retry shortly.")
    if outcome != "ok":
        raise HTTPException(status_code=503, detail="The runner could not queue this execution.")
    return {"job_id": job_id, "status": "queued", "created_at": now}


async def get_public_job(redis: Any, job_id: str, owner_id: str) -> dict[str, Any] | None:
    record = await redis.hgetall(f"codeleaf:runner:job:{job_id}")
    if not record or not hmac.compare_digest(record.get("owner_id", ""), owner_id):
        return None
    try:
        result = json.loads(record.get("result_json", "{}"))
    except (TypeError, json.JSONDecodeError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    return {
        "job_id": record.get("job_id", job_id),
        "status": record.get("status", "failed"),
        "created_at": record.get("created_at", ""),
        "updated_at": record.get("updated_at", ""),
        "result": result,
    }


async def cancel_job(redis: Any, job_id: str, owner_id: str) -> str | None:
    job_key = f"codeleaf:runner:job:{job_id}"
    record = await redis.hgetall(job_key)
    if not record or not hmac.compare_digest(record.get("owner_id", ""), owner_id):
        return None
    result = await redis.eval(
        CANCEL_JOB_LUA,
        2,
        job_key,
        f"codeleaf:runner:owner-active:{owner_id}",
        job_id,
        now_iso(),
    )
    return str(result[0])


async def set_job_running(
    redis: Any, job_id: str, *, worker_id: str = "worker", lease_seconds: int = 105
) -> dict[str, Any] | None:
    # Claim queued jobs atomically with cancellation. This prevents a worker from
    # starting a job after the API has already confirmed that it was cancelled.
    result = await redis.eval(
        CLAIM_JOB_LUA,
        2,
        f"codeleaf:runner:job:{job_id}",
        f"codeleaf:runner:job-lease:{job_id}",
        now_iso(),
        worker_id,
        lease_seconds,
    )
    state = str(result[0]) if result else "missing"
    if state == "missing":
        return None
    if state != "running":
        return {"status": state, "payload": None}
    try:
        payload = json.loads(result[1] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {"status": "running", "payload": payload, "owner_id": str(result[2] or "")}


async def finish_job(
    redis: Any,
    job_id: str,
    *,
    owner_id: str,
    status: str,
    result: dict[str, Any],
    ttl_seconds: int,
) -> None:
    job_key = f"codeleaf:runner:job:{job_id}"
    pipe = redis.pipeline(transaction=True)
    pipe.hset(
        job_key,
        mapping={
            "status": status,
            "updated_at": now_iso(),
            "result_json": json.dumps(result, separators=(",", ":"), ensure_ascii=False),
            "payload_json": "{}",
        },
    )
    pipe.srem(f"codeleaf:runner:owner-active:{owner_id}", job_id)
    pipe.delete(f"codeleaf:runner:job-lease:{job_id}")
    pipe.expire(job_key, ttl_seconds)
    await pipe.execute()
