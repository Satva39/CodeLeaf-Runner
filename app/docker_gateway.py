from __future__ import annotations

import hmac
import json
import os
import re
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

API_VERSION = os.getenv("DOCKER_GATEWAY_API_VERSION", "1.45")
API_PREFIX = f"/v{API_VERSION}"
TOKEN = os.getenv("RUNNER_DOCKER_GATEWAY_TOKEN", "")
MYSQL_IMAGE = os.getenv("RUNNER_DATABASE_MYSQL_IMAGE", "mysql:8.4.13")
POSTGRES_IMAGE = os.getenv("RUNNER_DATABASE_POSTGRES_IMAGE", "postgres:17.11-bookworm")
WORKER_NAME = os.getenv("RUNNER_DATABASE_WORKER_CONTAINER", "codeleaf-runner-database-worker")
SOCKET_PATH = os.getenv("RUNNER_DOCKER_GATEWAY_SOCKET", "/var/run/docker.sock")
HEX_ID_RE = re.compile(r"^[a-f0-9]{64}$")
NAME_RE = re.compile(r"^codeleaf-db-(mysql|postgresql)-([a-f0-9]{20})$")
NETWORK_RE = re.compile(r"^codeleaf-db-net-([a-f0-9]{20})$")


def _valid_token(value: str) -> bool:
    return len(TOKEN) >= 32 and bool(value) and hmac.compare_digest(value, TOKEN)


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def validate_network_create(body: Any) -> bool:
    if not isinstance(body, dict) or set(body) != {"Name", "Driver", "Internal", "CheckDuplicate", "Labels"}:
        return False
    match = NETWORK_RE.fullmatch(str(body.get("Name", "")))
    labels = body.get("Labels")
    if not match or body.get("Driver") != "bridge" or body.get("Internal") is not True or body.get("CheckDuplicate") is not True:
        return False
    if not isinstance(labels, dict) or set(labels) != {"codeleaf.runner.managed", "codeleaf.runner.database-job", "codeleaf.runner.created-at"}:
        return False
    job_id = str(labels.get("codeleaf.runner.database-job", ""))
    return (
        labels.get("codeleaf.runner.managed") == "true"
        and _is_uuid(job_id)
        and job_id.replace("-", "")[:20] == match.group(1)
        and str(labels.get("codeleaf.runner.created-at", "")).isdigit()
    )


def validate_database_container_config(name: str, body: Any, network_inspect: dict[str, Any], allowed_images: dict[str, str]) -> bool:
    match = NAME_RE.fullmatch(name)
    if not match or not isinstance(body, dict):
        return False
    engine, suffix = match.groups()
    expected_engine = "mysql" if engine == "mysql" else "postgresql"
    labels = body.get("Labels")
    host = body.get("HostConfig")
    network_name = f"codeleaf-db-net-{suffix}"
    network_labels = network_inspect.get("Labels") or {}
    network_id = str(network_inspect.get("Id", ""))
    required_top = {"Image", "Env", "Cmd", "Labels", "HostConfig", "StopTimeout"}
    if set(body) != required_top:
        return False
    if body.get("Image") != allowed_images.get(engine):
        return False
    if body.get("StopTimeout") != 2:
        return False
    job_id = labels.get("codeleaf.runner.database-job", "") if isinstance(labels, dict) else ""
    if not _is_uuid(job_id) or job_id.replace("-", "")[:20] != suffix:
        return False
    if labels != {
        "codeleaf.runner.managed": "true",
        "codeleaf.runner.database-job": job_id,
        "codeleaf.runner.database-engine": expected_engine,
        "codeleaf.runner.database-network-id": network_id,
    }:
        return False
    if network_labels.get("codeleaf.runner.managed") != "true" or network_labels.get("codeleaf.runner.database-job") != job_id or network_inspect.get("Internal") is not True:
        return False
    if set(network_labels) < {"codeleaf.runner.managed", "codeleaf.runner.database-job", "codeleaf.runner.created-at"}:
        return False
    if not isinstance(host, dict):
        return False
    expected_host_keys = {
        "NetworkMode", "Memory", "MemorySwap", "NanoCpus", "PidsLimit", "ReadonlyRootfs",
        "CapDrop", "CapAdd", "SecurityOpt", "Tmpfs", "Ulimits", "ShmSize",
        "RestartPolicy", "AutoRemove", "LogConfig",
    }
    if set(host) != expected_host_keys:
        return False
    if host.get("NetworkMode") != network_name or host.get("Memory") != 536870912 or host.get("MemorySwap") != 536870912:
        return False
    if host.get("NanoCpus") != 500000000 or host.get("PidsLimit") != 64 or host.get("ReadonlyRootfs") is not True:
        return False
    if host.get("CapDrop") != ["ALL"] or set(host.get("CapAdd", [])) != {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}:
        return False
    if host.get("SecurityOpt") != ["no-new-privileges:true"] or host.get("ShmSize") != 67108864:
        return False
    if host.get("AutoRemove") is not False or host.get("RestartPolicy") != {"Name": "no"}:
        return False
    if host.get("LogConfig") != {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}}:
        return False
    if host.get("Ulimits") != [
        {"Name": "nofile", "Soft": 256, "Hard": 256},
        {"Name": "nproc", "Soft": 64, "Hard": 64},
    ]:
        return False
    tmpfs = host.get("Tmpfs")
    allowed_tmpfs = {
        "/var/lib/postgresql/data": "rw,nosuid,nodev,noexec,size=256m",
        "/var/run/postgresql": "rw,nosuid,nodev,noexec,size=16m",
        "/var/lib/mysql": "rw,nosuid,nodev,noexec,size=256m",
        "/var/lib/mysql-files": "rw,nosuid,nodev,noexec,size=8m",
        "/var/run/mysqld": "rw,nosuid,nodev,noexec,size=16m",
        "/tmp": "rw,nosuid,nodev,noexec,size=32m",
    }
    if not isinstance(tmpfs, dict) or not tmpfs or any(allowed_tmpfs.get(k) != v for k, v in tmpfs.items()):
        return False

    env_list = body.get("Env")
    command = body.get("Cmd")
    if not isinstance(env_list, list) or not all(isinstance(item, str) and "\n" not in item and "\r" not in item for item in env_list):
        return False
    env = {}
    for item in env_list:
        key, sep, value = item.partition("=")
        if not sep or key in env:
            return False
        env[key] = value
    if engine == "postgresql":
        if set(env) != {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_INITDB_ARGS", "PGDATA"}:
            return False
        if env.get("POSTGRES_USER") != f"admin_{suffix}" or len(env.get("POSTGRES_PASSWORD", "")) < 32:
            return False
        if env.get("POSTGRES_DB") != "codeleaf" or env.get("POSTGRES_INITDB_ARGS") != "--auth-host=scram-sha-256":
            return False
        if env.get("PGDATA") != "/var/lib/postgresql/data/pgdata":
            return False
        expected_cmd = ["postgres", "-c", "listen_addresses=*", "-c", "max_connections=12", "-c", "statement_timeout=3000", "-c", "lock_timeout=750", "-c", "idle_in_transaction_session_timeout=3000", "-c", "temp_file_limit=32MB", "-c", "work_mem=4MB", "-c", "max_parallel_workers_per_gather=0", "-c", "log_destination=stderr"]
        if set(tmpfs) != {"/var/lib/postgresql/data", "/var/run/postgresql", "/tmp"}:
            return False
    else:
        if set(env) != {"MYSQL_ROOT_PASSWORD", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD"}:
            return False
        if len(env.get("MYSQL_ROOT_PASSWORD", "")) < 32 or len(env.get("MYSQL_PASSWORD", "")) < 32:
            return False
        if env.get("MYSQL_DATABASE") != "codeleaf" or env.get("MYSQL_USER") != f"learner_{suffix}":
            return False
        expected_cmd = ["--bind-address=0.0.0.0", "--max-connections=12", "--max-execution-time=3000", "--local-infile=0", "--secure-file-priv=/var/lib/mysql-files", "--performance-schema=OFF", "--skip-log-bin", "--max-allowed-packet=1M", "--innodb-buffer-pool-size=64M", "--innodb-log-buffer-size=8M", "--tmpdir=/tmp"]
        if set(tmpfs) != {"/var/lib/mysql", "/var/lib/mysql-files", "/var/run/mysqld", "/tmp"}:
            return False
    if command != expected_cmd:
        return False
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    if len(TOKEN) < 32:
        raise RuntimeError("RUNNER_DOCKER_GATEWAY_TOKEN must contain at least 32 characters.")
    transport = httpx.AsyncHTTPTransport(uds=SOCKET_PATH, retries=0)
    app.state.docker = httpx.AsyncClient(
        transport=transport,
        base_url="http://docker",
        timeout=httpx.Timeout(connect=4, read=300, write=30, pool=5),
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    )
    yield
    await app.state.docker.aclose()


app = FastAPI(title="CodeLeaf Docker Gateway", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


async def _docker_get(client: httpx.AsyncClient, path: str) -> tuple[int, dict[str, Any]]:
    response = await client.get(path)
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


@app.get("/health/live")
async def liveness():
    return {"status": "alive"}


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy_docker_api(path: str, request: Request):
    provided = request.headers.get("x-docker-gateway-key", "")
    if not _valid_token(provided):
        return JSONResponse({"message": "Docker gateway authentication failed."}, status_code=401)
    if len(TOKEN) < 32:
        return JSONResponse({"message": "Docker gateway is not configured securely."}, status_code=503)

    method = request.method
    route = "/" + path
    if route == "/_ping" and method == "GET":
        return Response("OK", media_type="text/plain")
    if not route.startswith(API_PREFIX + "/"):
        return JSONResponse({"message": "Docker API route is not allowed."}, status_code=403)
    suffix = route[len(API_PREFIX):]
    query = dict(request.query_params)
    client: httpx.AsyncClient = request.app.state.docker
    allowed = False
    body: Any = None

    if method == "GET":
        if suffix == f"/containers/{WORKER_NAME}/json" and not query:
            allowed = True
        elif suffix == "/containers/json":
            allowed = set(query) == {"all", "filters"} and query.get("all") == "1" and query.get("filters") == '{"label":["codeleaf.runner.managed=true"]}'
        elif suffix == "/networks":
            allowed = set(query) == {"filters"} and query.get("filters") == '{"label":["codeleaf.runner.managed=true"]}'
        elif suffix.startswith("/images/") and suffix.endswith("/json"):
            image_name = unquote(suffix[len("/images/"):-len("/json")])
            allowed = image_name in {MYSQL_IMAGE, POSTGRES_IMAGE} and not query
        elif re.fullmatch(r"/networks/[a-f0-9]{64}", suffix) and not query:
            network_id = suffix.split("/")[2]
            code, info = await _docker_get(client, f"{API_PREFIX}/networks/{network_id}")
            allowed = code == 200 and info.get("Internal") is True and (info.get("Labels") or {}).get("codeleaf.runner.managed") == "true"
        elif re.fullmatch(r"/containers/[A-Za-z0-9_.-]+/json", suffix) and not query:
            name = suffix.split("/")[2]
            if name.startswith("codeleaf-db-"):
                code, info = await _docker_get(client, f"{API_PREFIX}/containers/{name}/json")
                labels = info.get("Config", {}).get("Labels", {}) if isinstance(info.get("Config"), dict) else {}
                allowed = code == 200 and labels.get("codeleaf.runner.managed") == "true"
    elif method == "POST":
        body_bytes = await request.body()
        if len(body_bytes) > 262144:
            return JSONResponse({"message": "Docker gateway request is too large."}, status_code=413)
        if body_bytes:
            try:
                body = json.loads(body_bytes)
            except json.JSONDecodeError:
                return JSONResponse({"message": "Invalid Docker gateway JSON."}, status_code=400)
        if suffix == "/images/create":
            allowed = set(query) == {"fromImage"} and query.get("fromImage") in {MYSQL_IMAGE, POSTGRES_IMAGE} and not body_bytes
        elif suffix == "/networks/create":
            allowed = not query and validate_network_create(body)
        elif re.fullmatch(r"/networks/[a-f0-9]{64}/(connect|disconnect)", suffix) and not query:
            network_id, action = suffix.split("/")[2:]
            code, network = await _docker_get(client, f"{API_PREFIX}/networks/{network_id}")
            worker_code, worker = await _docker_get(client, f"{API_PREFIX}/containers/{WORKER_NAME}/json")
            network_labels = network.get("Labels") or {}
            worker_id = worker.get("Id")
            allowed = code == 200 and network.get("Internal") is True and network_labels.get("codeleaf.runner.managed") == "true" and worker_code == 200 and isinstance(worker_id, str) and isinstance(body, dict) and body.get("Container") == worker_id
            if action == "disconnect" and isinstance(body, dict):
                allowed = allowed and set(body) == {"Container", "Force"} and body.get("Force") is True
            else:
                allowed = allowed and isinstance(body, dict) and set(body) == {"Container"}
        elif re.fullmatch(r"/containers/[a-f0-9]{64}/start", suffix):
            container_id = suffix.split("/")[2]
            code, info = await _docker_get(client, f"{API_PREFIX}/containers/{container_id}/json")
            labels = info.get("Config", {}).get("Labels", {}) if isinstance(info.get("Config"), dict) else {}
            allowed = code == 200 and labels.get("codeleaf.runner.managed") == "true" and not body_bytes and not query
        elif suffix == "/containers/create":
            name = query.get("name", "") if set(query) == {"name"} else ""
            match = NAME_RE.fullmatch(name)
            if match and isinstance(body, dict):
                engine, suffix_id = match.groups()
                network_name = f"codeleaf-db-net-{suffix_id}"
                network_code, network_info = await _docker_get(client, f"{API_PREFIX}/networks/{network_name}")
                allowed = network_code == 200 and validate_database_container_config(name, body, network_info, {"mysql": MYSQL_IMAGE, "postgresql": POSTGRES_IMAGE})
    elif method == "DELETE":
        if re.fullmatch(r"/containers/[a-f0-9]{64}", suffix):
            container_id = suffix.split("/")[2]
            code, info = await _docker_get(client, f"{API_PREFIX}/containers/{container_id}/json")
            labels = info.get("Config", {}).get("Labels", {}) if isinstance(info.get("Config"), dict) else {}
            allowed = code == 200 and labels.get("codeleaf.runner.managed") == "true"
            allowed = allowed and query == {"force": "true", "v": "true"}
        elif re.fullmatch(r"/networks/[a-f0-9]{64}", suffix):
            network_id = suffix.split("/")[2]
            code, info = await _docker_get(client, f"{API_PREFIX}/networks/{network_id}")
            labels = info.get("Labels") or {}
            allowed = code == 200 and info.get("Internal") is True and labels.get("codeleaf.runner.managed") == "true" and not query

    if not allowed:
        return JSONResponse({"message": "Docker API operation is outside the CodeLeaf database-worker allowlist."}, status_code=403)

    try:
        headers = {}
        content = await request.body() if method == "POST" and body is None else None
        response = await client.request(method, route, params=query, content=content if content else (json.dumps(body).encode() if body is not None else None), headers=headers, timeout=300.0 if suffix == "/images/create" else None)
    except httpx.HTTPError:
        return JSONResponse({"message": "Docker Engine is unavailable through the restricted gateway."}, status_code=502)
    response_body = await response.aread()
    if len(response_body) > 16 * 1024 * 1024:
        return JSONResponse({"message": "Docker Engine response exceeded the gateway size limit."}, status_code=502)
    forwarded_headers = {}
    content_type = response.headers.get("content-type")
    if content_type:
        forwarded_headers["content-type"] = content_type
    return Response(content=response_body, status_code=response.status_code, headers=forwarded_headers)
