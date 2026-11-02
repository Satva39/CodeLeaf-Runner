from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_local_compose_exposes_only_loopback_proxies():
    compose = yaml.safe_load(
        (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")
    )

    services = compose["services"]

    assert set(services) == {"api-local-proxy", "piston-admin"}

    api_proxy = services["api-local-proxy"]
    assert api_proxy["ports"] == ["127.0.0.1:8080:8080"]
    assert api_proxy["profiles"] == ["runtime-admin"]
    assert set(api_proxy["networks"]) == {
        "runner-internal",
        "runner-local-ingress",
    }

    piston_admin = services["piston-admin"]
    assert piston_admin["ports"] == ["127.0.0.1:2000:2000"]
    assert set(piston_admin["networks"]) == {
        "runner-internal",
        "runner-local-ingress",
    }


def test_local_compose_does_not_publish_privileged_or_stateful_services():
    compose = yaml.safe_load(
        (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")
    )

    services = compose["services"]

    forbidden_services = {
        "api",
        "piston",
        "redis",
        "docker-gateway",
        "database-worker",
        "worker",
    }

    assert not (forbidden_services & set(services))

    for service in services.values():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:")

    assert compose["networks"]["runner-local-ingress"]["driver"] == "bridge"