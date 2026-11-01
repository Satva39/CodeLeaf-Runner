from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from .auth import require_runner_key
from .config import get_settings
from .jobs import cancel_job, enqueue_job, get_public_job, prepare_payload
from .models import OWNER_ID_RE, CreateJobRequest
from .piston import PistonClient, PistonUnavailable
from .registry import load_registry, public_language, runtime_matches


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
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
    }
    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "unavailable"
    try:
        runtimes = await piston.list_runtimes()
        checks["piston"] = f"ok:{len(runtimes)}_runtimes"
    except PistonUnavailable:
        checks["piston"] = "unavailable"
    ready = all(value not in {"missing_or_too_short", "unavailable"} for value in checks.values())
    if not ready:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}


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
