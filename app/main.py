from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, status
from fastapi.responses import PlainTextResponse

from .auth import require_runner_key
from .config import Settings, get_settings
from .jobs import cancel_job, enqueue_job, get_public_job, prepare_payload
from .interactive import handle_interactive_session
from .database_jobs import (
    DATABASE_WORKER_HEARTBEAT_KEY,
    cancel_database_job,
    enqueue_database_job,
    get_public_database_job,
    validate_database_payload,
)
from .database_models import CreateDatabaseJobRequest
from .models import OWNER_ID_RE, CreateJobRequest
from .piston import PistonClient, PistonUnavailable
from .registry import load_registry, public_language, runtime_matches
from .sessions import request_session_termination
from .security import RequestSecurityMiddleware
from .worker import WORKER_HEARTBEAT_KEY


def validate_api_startup_settings(settings: Settings) -> None:
    if not settings.service_token_ready:
        raise RuntimeError("CODELEAF_RUNNER_SERVICE_TOKEN must contain at least 32 characters.")
    if settings.production_mode and not settings.allowed_websocket_origins:
        raise RuntimeError(
            "RUNNER_ALLOWED_WEBSOCKET_ORIGINS must contain at least one exact HTTPS origin in production mode."
        )
    if settings.production_mode and any(
        not origin.startswith("https://") for origin in settings.allowed_websocket_origins
    ):
        raise RuntimeError("Production WebSocket origins must use HTTPS.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    validate_api_startup_settings(settings)
    try:
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover - guarded by the deployment dependency set.
        raise RuntimeError("The redis package is required to start CodeLeaf Runner API.") from exc
    redis = Redis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    piston = PistonClient(settings)
    app.state.redis = redis
    app.state.piston = piston
    try:
        yield
    finally:
        await piston.close()
        await redis.aclose()


app = FastAPI(
    title="CodeLeaf Runner API",
    description="Private, queued code execution API backed by a self-hosted Piston engine.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(RequestSecurityMiddleware)


def request_services(request: Request) -> tuple[Any, PistonClient]:
    redis = getattr(request.app.state, "redis", None)
    piston = getattr(request.app.state, "piston", None)
    if redis is None or piston is None:
        raise HTTPException(status_code=503, detail="Runner services are still starting.")
    return redis, piston


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "alive", "service": "codeleaf-runner"}


@app.get("/health/ready", tags=["health"])
async def readiness(request: Request) -> dict[str, Any]:
    settings = get_settings()
    redis, piston = request_services(request)
    checks: dict[str, str] = {
        "service_auth": "configured" if settings.service_token_ready else "missing_or_too_short",
        "redis": "unknown",
        "piston": "unknown",
        "execution_worker": "unknown",
    }
    if settings.database_execution_enabled:
        checks["database_worker"] = "unknown"
    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "unavailable"
    try:
        runtimes = await piston.list_runtimes()
        packages = await piston.list_packages()
        checks["piston"] = f"ok:{len(runtimes)}_runtimes:{len(packages)}_package_versions"
    except PistonUnavailable:
        checks["piston"] = "unavailable"
    try:
        checks["execution_worker"] = "ok" if await redis.get(WORKER_HEARTBEAT_KEY) else "unavailable"
    except Exception:
        checks["execution_worker"] = "unavailable"
    if settings.database_execution_enabled:
        try:
            checks["database_worker"] = "ok" if await redis.get(DATABASE_WORKER_HEARTBEAT_KEY) else "unavailable"
        except Exception:
            checks["database_worker"] = "unavailable"
    ready = all(value not in {"missing_or_too_short", "unavailable"} for value in checks.values())
    if not ready:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}


@app.get("/metrics", dependencies=[Depends(require_runner_key)], tags=["operations"])
async def metrics(request: Request) -> PlainTextResponse:
    """Prometheus-compatible operational metrics; never expose IDs or source code."""
    redis, _ = request_services(request)
    try:
        code_depth = int(await redis.xlen("codeleaf:runner:jobs"))
        database_depth = int(await redis.xlen("codeleaf:runner:database-jobs"))
        execution_worker = bool(await redis.get(WORKER_HEARTBEAT_KEY))
        database_worker = bool(await redis.get(DATABASE_WORKER_HEARTBEAT_KEY))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Operational metrics are temporarily unavailable.") from exc
    lines = [
        "# HELP codeleaf_runner_queue_depth Number of waiting execution jobs.",
        "# TYPE codeleaf_runner_queue_depth gauge",
        f'codeleaf_runner_queue_depth{{queue="code"}} {code_depth}',
        f'codeleaf_runner_queue_depth{{queue="database"}} {database_depth}',
        "# HELP codeleaf_runner_worker_up Worker heartbeat present (1) or absent (0).",
        "# TYPE codeleaf_runner_worker_up gauge",
        f'codeleaf_runner_worker_up{{worker="execution"}} {1 if execution_worker else 0}',
        f'codeleaf_runner_worker_up{{worker="database"}} {1 if database_worker else 0}',
    ]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/v1/languages", dependencies=[Depends(require_runner_key)], tags=["languages"])
async def languages(request: Request) -> dict[str, Any]:
    _, piston = request_services(request)
    try:
        runtimes = await piston.list_runtimes()
    except PistonUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    items = [
        public_language(entry, runtime_matches(entry, runtimes))
        for entry in load_registry().values()
    ]
    return {
        "provider": "codeleaf-runner",
        "engine": "piston",
        "count": len(items),
        "available_count": sum(1 for item in items if item["available"]),
        "languages": items,
    }


@app.get("/v1/runtime-status", dependencies=[Depends(require_runner_key)], tags=["languages"])
async def runtime_status(request: Request) -> dict[str, Any]:
    """Return operational counts from Piston without exposing its admin API."""
    _, piston = request_services(request)
    try:
        runtimes = await piston.list_runtimes()
        packages = await piston.list_packages()
    except PistonUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    items = [
        public_language(entry, runtime_matches(entry, runtimes))
        for entry in load_registry().values()
    ]
    installed_package_versions = sum(1 for item in packages if item.get("installed"))
    return {
        "provider": "codeleaf-runner",
        "engine": "piston",
        "registry_language_count": len(items),
        "installed_language_count": sum(1 for item in items if item["available"]),
        "installed_runtime_count": len(runtimes),
        "package_catalogue_version_count": len(packages),
        "installed_package_version_count": installed_package_versions,
        "languages": items,
    }


@app.post("/v1/jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_runner_key)], tags=["jobs"])
async def create_job(
    body: CreateJobRequest,
    request: Request,
    x_codeleaf_user: str = Header(default="", alias="X-CodeLeaf-User"),
) -> dict[str, str]:
    settings = get_settings()
    redis, piston = request_services(request)

    # The current CodeLeaf backend sends owner_id in the JSON body. New integrations
    # should use the authenticated X-CodeLeaf-User header. If both are present they
    # must agree, preventing accidental cross-user job creation.
    owner_id = x_codeleaf_user or (body.owner_id or "")
    if not owner_id:
        raise HTTPException(status_code=401, detail="CodeLeaf user scope is required.")
    if not OWNER_ID_RE.fullmatch(owner_id):
        raise HTTPException(status_code=400, detail="CodeLeaf user scope has an invalid format.")
    if x_codeleaf_user and body.owner_id and x_codeleaf_user != body.owner_id:
        raise HTTPException(status_code=400, detail="CodeLeaf user scope does not match the request owner.")

    entry, payload = prepare_payload(body, settings)
    try:
        runtimes = await piston.list_runtimes()
    except PistonUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    runtime = runtime_matches(entry, runtimes)
    if runtime is None:
        raise HTTPException(
            status_code=409,
            detail=f"{entry['name']} runtime is registered but has not been installed on this CodeLeaf Runner node.",
        )
    installed_version = str(runtime.get("version", ""))
    if body.version not in {"*", installed_version}:
        raise HTTPException(
            status_code=400,
            detail=f"Runtime version {body.version!r} is not installed. Available version: {installed_version or 'unknown'}.",
        )
    payload["version"] = installed_version if body.version == "*" else body.version

    return await enqueue_job(
        redis,
        owner_id=owner_id,
        language=entry["key"],
        payload=payload,
        settings=settings,
    )


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_runner_key)], tags=["jobs"])
async def read_job(
    job_id: UUID,
    request: Request,
    x_codeleaf_user: str = Header(default="", alias="X-CodeLeaf-User"),
) -> dict[str, Any]:
    if not x_codeleaf_user or not OWNER_ID_RE.fullmatch(x_codeleaf_user):
        raise HTTPException(status_code=401, detail="CodeLeaf user scope is required.")
    redis, _ = request_services(request)
    job_id_value = str(job_id)
    result = await get_public_job(redis, job_id_value, x_codeleaf_user)
    if result is None:
        raise HTTPException(status_code=404, detail="Job was not found.")
    return result


@app.delete("/v1/jobs/{job_id}", dependencies=[Depends(require_runner_key)], tags=["jobs"])
async def delete_job(
    job_id: UUID,
    request: Request,
    x_codeleaf_user: str = Header(default="", alias="X-CodeLeaf-User"),
) -> dict[str, str]:
    if not x_codeleaf_user or not OWNER_ID_RE.fullmatch(x_codeleaf_user):
        raise HTTPException(status_code=401, detail="CodeLeaf user scope is required.")
    redis, _ = request_services(request)
    job_id_value = str(job_id)
    result = await cancel_job(redis, job_id_value, x_codeleaf_user)
    if result is None or result == "missing":
        raise HTTPException(status_code=404, detail="Job was not found.")
    if result == "running":
        raise HTTPException(
            status_code=409,
            detail="This job is already running. Running-job termination will be added with interactive execution support.",
        )
    return {"job_id": job_id_value, "status": result}


@app.get("/v1/database/engines", dependencies=[Depends(require_runner_key)], tags=["databases"])
async def database_engines(request: Request) -> dict[str, Any]:
    settings = get_settings()
    redis, piston = request_services(request)
    worker_ready = bool(await redis.get(DATABASE_WORKER_HEARTBEAT_KEY)) if settings.database_execution_enabled else False
    sqlite_ready = False
    if worker_ready:
        try:
            sqlite_entry = load_registry()["sqlite"]
            sqlite_ready = runtime_matches(sqlite_entry, await piston.list_runtimes()) is not None
        except PistonUnavailable:
            sqlite_ready = False
    return {
        "provider": "codeleaf-runner",
        "engines": [
            {"key": "sqlite", "name": "SQLite", "available": bool(settings.database_execution_enabled and worker_ready and sqlite_ready), "mode": "isolated-piston-runtime"},
            {"key": "mysql", "name": "MySQL", "available": bool(settings.database_execution_enabled and worker_ready), "mode": "disposable-container"},
            {"key": "postgresql", "name": "PostgreSQL", "available": bool(settings.database_execution_enabled and worker_ready), "mode": "disposable-container"},
        ],
    }


@app.post("/v1/database/jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_runner_key)], tags=["databases"])
async def create_database_job(
    body: CreateDatabaseJobRequest,
    request: Request,
    x_codeleaf_user: str = Header(default="", alias="X-CodeLeaf-User"),
) -> dict[str, str]:
    settings = get_settings()
    if not settings.database_execution_enabled:
        raise HTTPException(status_code=503, detail="Database execution is disabled on this Runner node.")
    if not x_codeleaf_user or not OWNER_ID_RE.fullmatch(x_codeleaf_user):
        raise HTTPException(status_code=401, detail="CodeLeaf user scope is required.")
    redis, _ = request_services(request)
    if not await redis.get(DATABASE_WORKER_HEARTBEAT_KEY):
        raise HTTPException(status_code=503, detail="Database execution worker is not ready.")
    payload = validate_database_payload(body, settings)
    return await enqueue_database_job(redis, owner_id=x_codeleaf_user, payload=payload, settings=settings)


@app.get("/v1/database/jobs/{job_id}", dependencies=[Depends(require_runner_key)], tags=["databases"])
async def read_database_job(
    job_id: UUID,
    request: Request,
    x_codeleaf_user: str = Header(default="", alias="X-CodeLeaf-User"),
) -> dict[str, Any]:
    if not x_codeleaf_user or not OWNER_ID_RE.fullmatch(x_codeleaf_user):
        raise HTTPException(status_code=401, detail="CodeLeaf user scope is required.")
    redis, _ = request_services(request)
    result = await get_public_database_job(redis, str(job_id), x_codeleaf_user)
    if result is None:
        raise HTTPException(status_code=404, detail="Database job was not found.")
    return result


@app.delete("/v1/database/jobs/{job_id}", dependencies=[Depends(require_runner_key)], tags=["databases"])
async def delete_database_job(
    job_id: UUID,
    request: Request,
    x_codeleaf_user: str = Header(default="", alias="X-CodeLeaf-User"),
) -> dict[str, str]:
    if not x_codeleaf_user or not OWNER_ID_RE.fullmatch(x_codeleaf_user):
        raise HTTPException(status_code=401, detail="CodeLeaf user scope is required.")
    redis, _ = request_services(request)
    result = await cancel_database_job(redis, str(job_id), x_codeleaf_user)
    if result is None or result == "missing":
        raise HTTPException(status_code=404, detail="Database job was not found.")
    if result == "running":
        raise HTTPException(status_code=409, detail="This database job is already running and cannot be cancelled through the queue API.")
    return {"job_id": str(job_id), "status": result}


@app.websocket("/v1/sessions")
async def interactive_session(websocket: WebSocket) -> None:
    """Private interactive terminal proxy; the browser must connect through CodeLeaf backend."""
    await handle_interactive_session(websocket)


@app.delete("/v1/sessions/{session_id}", dependencies=[Depends(require_runner_key)], tags=["sessions"])
async def terminate_interactive_session(
    session_id: UUID,
    request: Request,
    x_codeleaf_user: str = Header(default="", alias="X-CodeLeaf-User"),
) -> dict[str, str]:
    if not x_codeleaf_user or not OWNER_ID_RE.fullmatch(x_codeleaf_user):
        raise HTTPException(status_code=401, detail="CodeLeaf user scope is required.")
    redis, _ = request_services(request)
    requested = await request_session_termination(
        redis, session_id=str(session_id), owner_id=x_codeleaf_user
    )
    if not requested:
        raise HTTPException(status_code=404, detail="Interactive session was not found.")
    return {"session_id": str(session_id), "status": "termination_requested"}
