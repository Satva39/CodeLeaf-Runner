from __future__ import annotations

from pathlib import Path

import yaml

from scripts.security_preflight import validate_compose, validate_env

ROOT = Path(__file__).resolve().parents[1]


def load_compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def secure_env():
    return {
        "CODELEAF_RUNNER_SERVICE_TOKEN": "s" * 48,
        "RUNNER_DOCKER_GATEWAY_TOKEN": "d" * 48,
        "CODELEAF_RUNNER_PREVIOUS_SERVICE_TOKEN": "",
    }


def test_compose_has_expected_security_boundaries():
    assert validate_compose(load_compose()) == []


def test_compose_rejects_published_piston_port():
    compose = load_compose()
    compose["services"]["piston"]["ports"] = ["0.0.0.0:2000:2000"]
    assert any("Piston must not publish" in item for item in validate_compose(compose))


def test_compose_rejects_database_worker_docker_socket_mount():
    compose = load_compose()
    compose["services"]["database-worker"]["volumes"] = ["/var/run/docker.sock:/var/run/docker.sock"]
    assert any("Only docker-gateway" in item for item in validate_compose(compose))


def test_compose_rejects_api_on_docker_control_network():
    compose = load_compose()
    compose["services"]["api"]["networks"].append("docker-control")
    assert any("API must not connect" in item for item in validate_compose(compose))


def test_compose_requires_internal_networks_and_piston_network_disable():
    compose = load_compose()
    compose["networks"]["runner-internal"]["internal"] = False
    compose["services"]["piston"]["environment"]["PISTON_DISABLE_NETWORKING"] = "false"
    errors = validate_compose(compose)
    assert any("runner-internal" in item for item in errors)
    assert any("Piston networking" in item for item in errors)


def test_production_env_requires_distinct_secrets_https_origins_and_image_digests():
    values = secure_env()
    values.update({
        "RUNNER_PRODUCTION_MODE": "true",
        "RUNNER_ALLOWED_WEBSOCKET_ORIGINS": "https://codeleaf.example",
        "PISTON_IMAGE": "piston@sha256:" + "a" * 64,
        "REDIS_IMAGE": "redis@sha256:" + "b" * 64,
        "RUNNER_DATABASE_MYSQL_IMAGE": "mysql@sha256:" + "c" * 64,
        "RUNNER_DATABASE_POSTGRES_IMAGE": "postgres@sha256:" + "d" * 64,
        "PISTON_ADMIN_IMAGE": "socat@sha256:" + "e" * 64,
        "RUNNER_PYTHON_BASE_IMAGE": "python@sha256:" + "f" * 64,
    })
    assert validate_env(values, production=True) == []


def test_production_env_rejects_missing_image_digests_and_http_origin():
    values = secure_env()
    values.update({"RUNNER_PRODUCTION_MODE": "true", "RUNNER_ALLOWED_WEBSOCKET_ORIGINS": "http://localhost:3000"})
    errors = validate_env(values, production=True)
    assert any("HTTPS origins" in item for item in errors)
    assert sum("immutable @sha256" in item for item in errors) == 6


def test_production_env_rejects_shared_service_and_gateway_secret():
    values = secure_env()
    values["RUNNER_DOCKER_GATEWAY_TOKEN"] = values["CODELEAF_RUNNER_SERVICE_TOKEN"]
    errors = validate_env(values, production=False)
    assert any("must be different" in item for item in errors)


def test_application_services_do_not_receive_full_env_file_or_gateway_secret():
    services = load_compose()["services"]
    assert "env_file" not in services["api"]
    assert "env_file" not in services["worker"]
    assert "RUNNER_DOCKER_GATEWAY_TOKEN" not in services["api"].get("environment", {})
    assert "RUNNER_DOCKER_GATEWAY_TOKEN" not in services["worker"].get("environment", {})
    assert "CODELEAF_RUNNER_SERVICE_TOKEN" not in services["worker"].get("environment", {})
    assert "CODELEAF_RUNNER_PREVIOUS_SERVICE_TOKEN" not in services["worker"].get("environment", {})
    assert "CODELEAF_RUNNER_SERVICE_TOKEN" not in services["database-worker"].get("environment", {})
    assert "CODELEAF_RUNNER_SERVICE_TOKEN" not in services["docker-gateway"].get("environment", {})
    assert "RUNNER_DOCKER_GATEWAY_TOKEN" in services["database-worker"].get("environment", {})
    assert "RUNNER_DOCKER_GATEWAY_TOKEN" in services["docker-gateway"].get("environment", {})


def test_compose_requires_allowlisted_secrets_and_private_env_loading():
    compose = load_compose()
    compose["services"]["api"]["env_file"] = [".env"]
    errors = validate_compose(compose)
    assert any("explicit environment allowlist" in item for item in errors)


def test_secret_ignore_rules_allow_only_example_env():
    from scripts.security_preflight import validate_secret_ignore
    assert validate_secret_ignore(ROOT / ".gitignore") == []


def test_compose_keeps_docker_gateway_off_runner_internal_network():
    compose = load_compose()
    compose["services"]["docker-gateway"]["networks"].append("runner-internal")
    assert any("Docker gateway must only join" in item for item in validate_compose(compose))


def test_compose_requires_nonroot_locked_down_admin_sidecar():
    compose = load_compose()
    compose["services"]["piston-admin"]["user"] = "0:0"
    assert any("admin sidecar must run as non-root" in item for item in validate_compose(compose))
