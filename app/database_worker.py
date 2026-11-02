from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Any

from .config import get_settings
from .database_execution import execute_database_job
from .database_jobs import (
    DATABASE_GROUP,
    DATABASE_STREAM,
    DATABASE_WORKER_HEARTBEAT_KEY,
    claim_database_job,
    finish_database_job,
)
from .docker_engine import DockerEngine
from .piston import PistonClient

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("codeleaf-runner.database-worker")


async def ensure_consumer_group(redis: Any, response_error_type: type[Exception]) -> None:
    try:
        await redis.xgroup_create(DATABASE_STREAM, DATABASE_GROUP, id="0", mkstream=True)
    except response_error_type as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def acknowledge(redis: Any, stream_id: str) -> None:
    pipe = redis.pipeline(transaction=True)
    pipe.xack(DATABASE_STREAM, DATABASE_GROUP, stream_id)
    pipe.xdel(DATABASE_STREAM, stream_id)
    await pipe.execute()


async def process_message(
    redis: Any, docker: DockerEngine, piston: PistonClient,
    stream_id: str, fields: dict[str, Any], worker_id: str = "database-worker",
) -> None:
    settings = get_settings()
    job_id = str(fields.get("job_id", ""))
    if not job_id:
        await acknowledge(redis, stream_id)
        return
    lease_seconds = max(90, int(settings.database_job_timeout_seconds * 2) + 15)
    claimed = await claim_database_job(redis, job_id, worker_id=worker_id, lease_seconds=lease_seconds)
    if claimed is None:
        await acknowledge(redis, stream_id)
        return
    if claimed.get("status") == "busy":
        return
    payload = claimed["payload"]
    owner_id = claimed["owner_id"]
    try:
        result = await asyncio.wait_for(
            execute_database_job(
                engine=str(payload.get("engine", "")),
                setup_statements=payload.get("setup_statements", []),
                query=str(payload.get("query", "")),
                job_id=job_id,
                settings=settings,
                docker=docker,
                piston=piston,
            ),
            timeout=settings.database_job_timeout_seconds,
        )
        status = "completed"
    except asyncio.TimeoutError:
        result = {
            "engine": str(payload.get("engine", "")), "columns": [], "rows": [],
            "row_count": 0, "affected_rows": None, "execution_ms": int(settings.database_job_timeout_seconds * 1000),
            "truncated": False, "message": None,
            "error": "Database sandbox exceeded the job time limit and was stopped.",
        }
        status = "failed"
    except Exception:
        logger.exception("Database job failed: %s", job_id)
        result = {
            "engine": str(payload.get("engine", "")), "columns": [], "rows": [],
            "row_count": 0, "affected_rows": None, "execution_ms": 0,
            "truncated": False, "message": None,
            "error": "The database runner could not complete this job.",
        }
        status = "failed"
    try:
        await finish_database_job(
            redis, job_id, owner_id=owner_id, status=status,
            result=result, settings=settings,
        )
    finally:
        await acknowledge(redis, stream_id)


async def run_database_worker() -> None:
    # This worker consumes jobs from the private Redis queue and talks to Docker
    # and Piston. It does not serve authenticated API requests, so it must not
    # require or receive the public Runner service token.
    settings = get_settings()
    try:
        from redis.asyncio import Redis
        from redis.exceptions import ResponseError
    except ImportError as exc:  # pragma: no cover - guarded by runtime dependencies.
        raise RuntimeError("The redis package is required for database execution.") from exc
    redis = Redis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    if not settings.docker_gateway_token_ready:
        raise RuntimeError("RUNNER_DOCKER_GATEWAY_TOKEN must contain at least 32 characters.")
    docker = DockerEngine(
        settings.database_docker_socket,
        settings.database_docker_api_version,
        settings.docker_gateway_token_value,
    )
    piston = PistonClient(settings)
    await redis.ping()
    await docker.verify_host(settings.database_worker_container_name)
    # Pull the fixed, administrator-configured database images before advertising
    # readiness. First learner jobs must not spend their timeout waiting on image pulls.
    await docker.ensure_image(settings.database_mysql_image)
    await docker.ensure_image(settings.database_postgres_image)
    await ensure_consumer_group(redis, ResponseError)
    concurrency = settings.database_worker_concurrency
    semaphore = asyncio.Semaphore(concurrency)
    consumer = f"database-{socket.gethostname()}-{os.getpid()}"
    active_tasks: set[asyncio.Task] = set()
    claim_cursor = "0-0"
    claim_idle_ms = max(60000, int(settings.database_job_timeout_seconds * 3000))

    async def heartbeat() -> None:
        while True:
            await redis.set(DATABASE_WORKER_HEARTBEAT_KEY, str(time.time()), ex=30)
            await asyncio.sleep(8)

    async def cleanup_orphaned_containers() -> None:
        # A worker process can be killed after Docker started a DB container.
        # Remove finished/missing-job sandboxes and time-expired orphans.
        while True:
            try:
                now = time.time()
                containers = await docker.list_managed_database_containers()
                for container in containers:
                    labels = container.get("Labels") or {}
                    job_id = str(labels.get("codeleaf.runner.database-job", ""))
                    container_id = str(container.get("Id", ""))
                    network_id = str(labels.get("codeleaf.runner.database-network-id", ""))
                    created = float(container.get("Created", 0) or 0)
                    record = await redis.hgetall(f"codeleaf:runner:database-job:{job_id}") if job_id else {}
                    status = record.get("status", "missing")
                    expired_running = status == "running" and created > 0 and now - created > settings.database_job_timeout_seconds + 30
                    if not job_id or status not in {"queued", "running"} or expired_running:
                        if container_id or network_id:
                            await docker.remove_ephemeral_database(
                                container_id or None,
                                network_id or None,
                                settings.database_worker_container_name,
                            )
                # Also handle a crash after network creation but before container creation.
                for network in await docker.list_managed_database_networks():
                    labels = network.get("Labels") or {}
                    job_id = str(labels.get("codeleaf.runner.database-job", ""))
                    network_id = str(network.get("Id", ""))
                    created = float(labels.get("codeleaf.runner.created-at", 0) or 0)
                    record = await redis.hgetall(f"codeleaf:runner:database-job:{job_id}") if job_id else {}
                    status = record.get("status", "missing")
                    expired_running = status == "running" and created > 0 and now - created > settings.database_job_timeout_seconds + 30
                    if network_id and (not job_id or status not in {"queued", "running"} or expired_running):
                        await docker.remove_ephemeral_database(
                            None, network_id, settings.database_worker_container_name
                        )
            except Exception as exc:
                logger.warning("Database orphan cleanup will retry: %s", type(exc).__name__)
            await asyncio.sleep(30)

    heartbeat_task = asyncio.create_task(heartbeat(), name="database-worker-heartbeat")
    cleanup_task = asyncio.create_task(cleanup_orphaned_containers(), name="database-orphan-cleanup")

    async def handle(stream_id: str, fields: dict[str, Any]) -> None:
        async with semaphore:
            await process_message(redis, docker, piston, stream_id, fields, consumer)

    logger.info("Database worker started; concurrency=%s", concurrency)
    try:
        while True:
            finished = {task for task in active_tasks if task.done()}
            for task in finished:
                active_tasks.remove(task)
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Database worker task exited unexpectedly")

            free_slots = concurrency - len(active_tasks)
            if free_slots > 0:
                try:
                    reclaimed = await redis.xautoclaim(
                        DATABASE_STREAM, DATABASE_GROUP, consumer,
                        min_idle_time=claim_idle_ms, start_id=claim_cursor, count=free_slots,
                    )
                    if reclaimed and len(reclaimed) >= 2:
                        claim_cursor = reclaimed[0] or "0-0"
                        for stream_id, fields in reclaimed[1]:
                            task = asyncio.create_task(handle(stream_id, fields))
                            active_tasks.add(task)
                    if reclaimed and reclaimed[0] == "0-0":
                        claim_cursor = "0-0"
                except ResponseError as exc:
                    logger.warning("Database pending-job recovery unavailable: %s", exc)

            free_slots = concurrency - len(active_tasks)
            if free_slots <= 0:
                await asyncio.wait(active_tasks, return_when=asyncio.FIRST_COMPLETED)
                continue
            messages = await redis.xreadgroup(
                DATABASE_GROUP, consumer, {DATABASE_STREAM: ">"}, count=free_slots, block=1000,
            )
            for _stream, entries in messages:
                for stream_id, fields in entries:
                    task = asyncio.create_task(handle(stream_id, fields))
                    active_tasks.add(task)
    finally:
        heartbeat_task.cancel()
        cleanup_task.cancel()
        await asyncio.gather(heartbeat_task, cleanup_task, return_exceptions=True)
        await redis.delete(DATABASE_WORKER_HEARTBEAT_KEY)
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        await piston.close()
        await docker.close()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run_database_worker())
