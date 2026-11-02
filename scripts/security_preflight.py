#!/usr/bin/env python3
"""Validate CodeLeaf Runner's production secrets and Docker Compose trust boundaries."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

try:
    import yaml
except ImportError as exc:  # pragma: no cover - operator dependency guard.
    raise SystemExit("PyYAML is required. Install with: python -m pip install -e '.[test]'") from exc

ROOT = Path(__file__).resolve().parents[1]
DIGEST_RE = re.compile(r"@sha256:[a-f0-9]{64}$")
PLACEHOLDERS = {"", "CHANGE_ME_BEFORE_DEPLOY", "GENERATE_A_DIFFERENT_RANDOM_SECRET_OF_AT_LEAST_32_CHARACTERS"}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _has_digest(value: str) -> bool:
    return bool(DIGEST_RE.search(value))


def _is_exact_https_origin(value: str) -> bool:
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and "*" not in value
    )


def validate_compose(compose: dict) -> list[str]:
    errors: list[str] = []
    services = compose.get("services") or {}
    networks = compose.get("networks") or {}
    required = {"redis", "piston", "piston-admin", "api", "worker", "docker-gateway", "database-worker"}
    if not required.issubset(services):
        return ["Compose is missing services: " + ", ".join(sorted(required - set(services)))]

    if not networks.get("runner-internal", {}).get("internal"):
        errors.append("runner-internal must be an internal Docker network.")
    if not networks.get("docker-control", {}).get("internal"):
        errors.append("docker-control must be an internal Docker network.")

    def ports(service: str) -> list[str]:
        return [str(item) for item in services[service].get("ports", [])]

    if ports("piston"):
        errors.append("Piston must not publish host ports in the normal stack.")
    if ports("docker-gateway"):
        errors.append("Docker gateway must not publish host ports.")
    if ports("redis"):
        errors.append("Redis must not publish host ports.")
    if ports("api") != ["127.0.0.1:8080:8080"]:
        errors.append("Runner API must bind only to 127.0.0.1:8080 by default.")
    if ports("piston-admin") != ["127.0.0.1:2000:2000"]:
        errors.append("Piston admin sidecar must bind only to 127.0.0.1:2000.")
    if "runtime-admin" not in services["piston-admin"].get("profiles", []):
        errors.append("Piston admin sidecar must remain opt-in under the runtime-admin profile.")
    admin = services["piston-admin"]
    if admin.get("read_only") is not True or admin.get("cap_drop") != ["ALL"]:
        errors.append("Piston admin sidecar must use a read-only filesystem and drop all capabilities.")
    if admin.get("user") in {None, "0", "0:0", 0}:
        errors.append("Piston admin sidecar must run as non-root.")

    socket_mounts = []
    for service_name, service in services.items():
        for mount in service.get("volumes", []):
            mount_text = mount if isinstance(mount, str) else str(mount)
            if "docker.sock" in mount_text:
                socket_mounts.append(service_name)
    if socket_mounts != ["docker-gateway"]:
        errors.append("Only docker-gateway may mount the host Docker socket (found: " + ", ".join(socket_mounts) + ").")

    database_worker = services["database-worker"]
    if database_worker.get("user") in {None, "0", "0:0", 0}:
        errors.append("database-worker must run as a non-root user.")
    if database_worker.get("read_only") is not True:
        errors.append("database-worker root filesystem must be read-only.")
    if database_worker.get("privileged"):
        errors.append("database-worker must not be privileged.")
    if "docker-control" not in database_worker.get("networks", []):
        errors.append("database-worker must connect to the private docker-control network.")
    if "docker-control" in services["api"].get("networks", []):
        errors.append("API must not connect to docker-control.")
    if "docker-control" in services["worker"].get("networks", []):
        errors.append("Execution worker must not connect to docker-control.")
    if set(services["redis"].get("networks", [])) != {"runner-internal"}:
        errors.append("Redis must only join runner-internal.")
    if set(services["api"].get("networks", [])) != {"runner-internal"}:
        errors.append("API must only join runner-internal.")
    if set(services["worker"].get("networks", [])) != {"runner-internal"}:
        errors.append("Execution worker must only join runner-internal.")
    if set(services["docker-gateway"].get("networks", [])) != {"docker-control"}:
        errors.append("Docker gateway must only join docker-control.")
    if set(database_worker.get("networks", [])) != {"runner-internal", "docker-control"}:
        errors.append("Database worker must join only runner-internal and docker-control.")
    for app_name in ("api", "worker", "database-worker", "docker-gateway"):
        service = services[app_name]
        if service.get("read_only") is not True:
            errors.append(f"{app_name} root filesystem must be read-only.")
        if service.get("cap_drop") != ["ALL"]:
            errors.append(f"{app_name} must drop all Linux capabilities.")
        if "no-new-privileges:true" not in service.get("security_opt", []):
            errors.append(f"{app_name} must enable no-new-privileges.")
    for app_name in ("api", "worker"):
        if services[app_name].get("env_file"):
            errors.append(f"{app_name} must use an explicit environment allowlist, not env_file.")
    if "RUNNER_DOCKER_GATEWAY_TOKEN" in services["api"].get("environment", {}):
        errors.append("API must not receive the Docker-gateway secret.")
    if "CODELEAF_RUNNER_SERVICE_TOKEN" in services["worker"].get("environment", {}):
        errors.append("Execution worker must not receive the Runner service token.")
    if "CODELEAF_RUNNER_PREVIOUS_SERVICE_TOKEN" in services["worker"].get("environment", {}):
        errors.append("Execution worker must not receive the previous Runner service token.")
    for service_name in ("database-worker", "docker-gateway", "piston", "redis"):
        if "CODELEAF_RUNNER_SERVICE_TOKEN" in services[service_name].get("environment", {}):
            errors.append(f"{service_name} must not receive the Runner service token.")
    if "RUNNER_DOCKER_GATEWAY_TOKEN" not in services["database-worker"].get("environment", {}):
        errors.append("Database worker must receive the gateway credential explicitly.")
    if services["piston"].get("privileged") is not True:
        errors.append("Piston must retain the privileged flag required by the configured Isolate/cgroup setup.")
    piston_env = services["piston"].get("environment", {})
    if str(piston_env.get("PISTON_DISABLE_NETWORKING", "")).lower() != "true":
        errors.append("Piston networking must be disabled.")
    if set(services["piston"].get("networks", [])) != {"runner-internal"}:
        errors.append("Piston must only join runner-internal.")
    return errors


def validate_env(values: dict[str, str], production: bool) -> list[str]:
    errors: list[str] = []
    service_token = values.get("CODELEAF_RUNNER_SERVICE_TOKEN", "")
    docker_token = values.get("RUNNER_DOCKER_GATEWAY_TOKEN", "")
    previous = values.get("CODELEAF_RUNNER_PREVIOUS_SERVICE_TOKEN", "")
    if len(service_token) < 32 or service_token in PLACEHOLDERS:
        errors.append("CODELEAF_RUNNER_SERVICE_TOKEN must be a generated secret of at least 32 characters.")
    if len(docker_token) < 32 or docker_token in PLACEHOLDERS:
        errors.append("RUNNER_DOCKER_GATEWAY_TOKEN must be a different generated secret of at least 32 characters.")
    if service_token and docker_token and service_token == docker_token:
        errors.append("Runner service and Docker gateway tokens must be different secrets.")
    if previous and (len(previous) < 32 or previous in PLACEHOLDERS or previous == service_token):
        errors.append("CODELEAF_RUNNER_PREVIOUS_SERVICE_TOKEN must be empty or a distinct generated secret of at least 32 characters.")

    if production:
        origins = [item.strip() for item in values.get("RUNNER_ALLOWED_WEBSOCKET_ORIGINS", "").split(",") if item.strip()]
        if not origins or any(not _is_exact_https_origin(item) for item in origins):
            errors.append("Production requires exact HTTPS origins in RUNNER_ALLOWED_WEBSOCKET_ORIGINS (no wildcards or paths).")
        for key in (
            "PISTON_IMAGE",
            "REDIS_IMAGE",
            "RUNNER_DATABASE_MYSQL_IMAGE",
            "RUNNER_DATABASE_POSTGRES_IMAGE",
            "PISTON_ADMIN_IMAGE",
            "RUNNER_PYTHON_BASE_IMAGE",
        ):
            value = values.get(key, "")
            if not _has_digest(value):
                errors.append(f"{key} must be pinned to an immutable @sha256 image digest in production.")
        if values.get("RUNNER_PRODUCTION_MODE", "").lower() not in {"true", "1", "yes"}:
            errors.append("Set RUNNER_PRODUCTION_MODE=true after production configuration is complete.")
    return errors


def validate_secret_ignore(ignore_path: Path) -> list[str]:
    if not ignore_path.exists():
        return [".gitignore is missing; the private .env file must not be tracked."]
    entries = {line.strip() for line in ignore_path.read_text(encoding="utf-8").splitlines()}
    if ".env" not in entries or ".env.*" not in entries or "!.env.example" not in entries:
        return [".gitignore must ignore .env and .env.* while allowing .env.example."]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(ROOT / ".env"), help="Environment file to inspect (never printed).")
    parser.add_argument("--compose-file", default=str(ROOT / "docker-compose.yml"))
    parser.add_argument("--production", action="store_true", help="Require production-only settings and immutable image digests.")
    args = parser.parse_args()

    values = read_env(Path(args.env_file))
    production = args.production or values.get("RUNNER_PRODUCTION_MODE", "").lower() in {"true", "1", "yes"}
    errors = validate_env(values, production)
    errors.extend(validate_secret_ignore(ROOT / ".gitignore"))
    try:
        compose = yaml.safe_load(Path(args.compose_file).read_text(encoding="utf-8"))
        if not isinstance(compose, dict):
            raise ValueError("Compose document must be a YAML object.")
        errors.extend(validate_compose(compose))
    except Exception as exc:
        errors.append(f"Could not validate Docker Compose configuration: {type(exc).__name__}: {exc}")

    if errors:
        print("CodeLeaf Runner security preflight: FAILED")
        for item in errors:
            print(f"  - {item}")
        return 1
    print("CodeLeaf Runner security preflight: PASSED")
    print("  - Secrets are present and distinct (values were not displayed).")
    print("  - Docker socket is confined to the allowlisted gateway.")
    print("  - API, Piston, Redis, workers and database networks match the checked boundaries.")
    if production:
        print("  - Production origins and image digests are configured.")
    else:
        print("  - Development mode: immutable production image digest checks were not requested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
