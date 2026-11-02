from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from typing import Any


from .config import get_settings
from .jobs import CONSUMER_GROUP, STREAM_KEY, finish_job, set_job_running
from .piston import PistonClient, PistonRejected, PistonUnavailable
from .results import normalize_piston_result

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("codeleaf-runner.worker")
WORKER_HEARTBEAT_KEY = "codeleaf:runner:worker:heartbeat"


async def ensure_consumer_group(redis: Any, response_error_type: type[Exception]) -> None:
    try:
        await redis.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0-0", mkstream=True)
    except response_error_type as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def acknowledge_and_delete(redis: Any, message_id: str) -> None:
    # Keep stream length bounded even if the worker is interrupted during cleanup.
    pipeline = redis.pipeline(transaction=True)
    pipeline.xack(STREAM_KEY, CONSUMER_GROUP, message_id)
    pipeline.xdel(STREAM_KEY, message_id)
    await pipeline.execute()


async def process_message(
    redis: Any,
    piston: PistonClient,
    settings,
    message_id: str,
    fields: dict[str, Any],
    worker_id: str = "worker",
) -> None:
    job_id = str(fields.get("job_id", ""))
    if not job_id:
        await acknowledge_and_delete(redis, message_id)
        return

    lease_seconds = max(90, int(settings.piston_http_timeout_seconds * 2) + 15)
    job = await set_job_running(redis, job_id, worker_id=worker_id, lease_seconds=lease_seconds)
    if not job:
        await acknowledge_and_delete(redis, message_id)
        return
    if job["status"] == "busy":
        # Another worker still holds the execution lease. Keep the stream item
        # pending; it will become reclaimable after the lease expires.
        return
    if job["status"] != "running":
        # Includes a job cancelled while it was still in the queue.
        await acknowledge_and_delete(redis, message_id)
        return

    payload = job.get("payload")
    owner_id = job.get("owner_id", "")
    if not isinstance(payload, dict) or not owner_id:
        await finish_job(
            redis,
            job_id,
            owner_id=owner_id,
            status="failed",
            result={"provider": "codeleaf-runner", "error": "The queued job payload was invalid."},
            ttl_seconds=settings.job_ttl_seconds,
        )
        await acknowledge_and_delete(redis, message_id)
        return

    started = time.perf_counter()
    try:
        response = await piston.execute(
            language=payload["piston_language"],
            version=payload["version"],
            files=payload["files"],
            stdin=payload.get("stdin", ""),
            args=payload.get("args", []),
        )
        result = normalize_piston_result(response, int((time.perf_counter() - started) * 1000))
        await finish_job(
            redis,
            job_id,
            owner_id=owner_id,
            status="completed",
            result=result,
            ttl_seconds=settings.job_ttl_seconds,
        )
    except PistonRejected as exc:
        await finish_job(
            redis,
            job_id,
            owner_id=owner_id,
            status="failed",
            result={"provider": "codeleaf-runner", "engine": "piston", "outcome": "rejected", "error": str(exc)[:300]},
            ttl_seconds=settings.job_ttl_seconds,
        )
    except PistonUnavailable as exc:
        await finish_job(
            redis,
            job_id,
            owner_id=owner_id,
            status="failed",
            result={"provider": "codeleaf-runner", "engine": "piston", "outcome": "engine_error", "error": str(exc)[:300]},
            ttl_seconds=settings.job_ttl_seconds,
        )
    except asyncio.CancelledError:
        # Leave the stream entry pending. Another worker can reclaim it after the idle lease.
        raise
    except Exception as exc:  # noqa: BLE001 - prevent a worker crash from losing a job.
        logger.exception("Unexpected execution worker failure for job %s (%s)", job_id, type(exc).__name__)
        await finish_job(
            redis,
            job_id,
            owner_id=owner_id,
            status="failed",
            result={"provider": "codeleaf-runner", "error": "An internal execution worker error occurred."},
            ttl_seconds=settings.job_ttl_seconds,
        )
    finally:
        # Acknowledge only once the job record has been finalized.
        record = await redis.hgetall(f"codeleaf:runner:job:{job_id}")
        if record and record.get("status") not in {"queued", "running"}:
            await acknowledge_and_delete(redis, message_id)


async def run_worker() -> None:
    settings = get_settings()

    try:
        from redis.asyncio import Redis
        from redis.exceptions import ResponseError
    except ImportError as exc:  # pragma: no cover - deployment dependency guard.
        raise RuntimeError("The redis package is required to start CodeLeaf Runner worker.") from exc
    redis = Redis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    piston = PistonClient(settings)
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    active_tasks: set[asyncio.Task] = set()
    claim_idle_ms = max(60000, int(settings.piston_http_timeout_seconds * 3000))
    claim_cursor = "0-0"

    async def heartbeat() -> None:
        while True:
            await redis.set(WORKER_HEARTBEAT_KEY, str(time.time()), ex=30)
            await asyncio.sleep(8)

    heartbeat_task = asyncio.create_task(heartbeat(), name="execution-worker-heartbeat")
    try:
        await redis.ping()
        await ensure_consumer_group(redis, ResponseError)
        logger.info("CodeLeaf Runner worker started; concurrency=%s", settings.worker_concurrency)

        while True:
            # Retrieve task exceptions before scheduling more work.
            completed_tasks = {task for task in active_tasks if task.done()}
            for task in completed_tasks:
                active_tasks.remove(task)
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Worker task exited unexpectedly")

            # Recover messages left pending by a worker process that stopped unexpectedly.
            free_slots = settings.worker_concurrency - len(active_tasks)
            try:
                reclaimed = await redis.xautoclaim(
                    STREAM_KEY,
                    CONSUMER_GROUP,
                    consumer,
                    min_idle_time=claim_idle_ms,
                    start_id=claim_cursor,
                    count=free_slots,
                ) if free_slots > 0 else None
                if reclaimed and len(reclaimed) >= 2:
                    claim_cursor = reclaimed[0] or "0-0"
                    for message_id, fields in reclaimed[1]:
                        task = asyncio.create_task(process_message(redis, piston, settings, message_id, fields, consumer))
                        active_tasks.add(task)
                if reclaimed and reclaimed[0] == "0-0":
                    claim_cursor = "0-0"
            except ResponseError as exc:
                logger.warning("Pending-job recovery temporarily unavailable: %s", exc)

            free_slots = settings.worker_concurrency - len(active_tasks)
            if free_slots <= 0:
                await asyncio.wait(active_tasks, return_when=asyncio.FIRST_COMPLETED)
                continue

            messages = await redis.xreadgroup(
                CONSUMER_GROUP,
                consumer,
                streams={STREAM_KEY: ">"},
                count=free_slots,
                block=1000,
            )
            for _, entries in messages:
                for message_id, fields in entries:
                    task = asyncio.create_task(process_message(redis, piston, settings, message_id, fields, consumer))
                    active_tasks.add(task)

    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        await redis.delete(WORKER_HEARTBEAT_KEY)
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        await piston.close()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run_worker())
