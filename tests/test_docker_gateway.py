from __future__ import annotations

from app.docker_gateway import validate_database_container_config, validate_network_create

JOB = "12345678-1234-4234-8234-123456789012"
SUFFIX = JOB.replace("-", "")[:20]
NETWORK_ID = "a" * 64


def network_body():
    return {
        "Name": f"codeleaf-db-net-{SUFFIX}",
        "Driver": "bridge",
        "Internal": True,
        "CheckDuplicate": True,
        "Labels": {
            "codeleaf.runner.managed": "true",
            "codeleaf.runner.database-job": JOB,
            "codeleaf.runner.created-at": "1793600000",
        },
    }


def mysql_config():
    return {
        "Image": "mysql:8.4.13",
        "Env": [
            "MYSQL_ROOT_PASSWORD=" + "r" * 44,
            "MYSQL_DATABASE=codeleaf",
            f"MYSQL_USER=learner_{SUFFIX}",
            "MYSQL_PASSWORD=" + "p" * 44,
        ],
        "Cmd": [
            "--bind-address=0.0.0.0", "--max-connections=12", "--max-execution-time=3000",
            "--local-infile=0", "--secure-file-priv=/var/lib/mysql-files",
            "--performance-schema=OFF", "--skip-log-bin", "--max-allowed-packet=1M",
            "--innodb-buffer-pool-size=64M", "--innodb-log-buffer-size=8M", "--tmpdir=/tmp",
        ],
        "Labels": {
            "codeleaf.runner.managed": "true",
            "codeleaf.runner.database-job": JOB,
            "codeleaf.runner.database-engine": "mysql",
            "codeleaf.runner.database-network-id": NETWORK_ID,
        },
        "HostConfig": {
            "NetworkMode": f"codeleaf-db-net-{SUFFIX}",
            "Memory": 536870912,
            "MemorySwap": 536870912,
            "NanoCpus": 500000000,
            "PidsLimit": 64,
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "CapAdd": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Tmpfs": {
                "/var/lib/mysql": "rw,nosuid,nodev,noexec,size=256m",
                "/var/lib/mysql-files": "rw,nosuid,nodev,noexec,size=8m",
                "/var/run/mysqld": "rw,nosuid,nodev,noexec,size=16m",
                "/tmp": "rw,nosuid,nodev,noexec,size=32m",
            },
            "Ulimits": [
                {"Name": "nofile", "Soft": 256, "Hard": 256},
                {"Name": "nproc", "Soft": 64, "Hard": 64},
            ],
            "ShmSize": 67108864,
            "RestartPolicy": {"Name": "no"},
            "AutoRemove": False,
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}},
        },
        "StopTimeout": 2,
    }


def network_inspect():
    return {
        "Id": NETWORK_ID,
        "Internal": True,
        "Labels": {
            "codeleaf.runner.managed": "true",
            "codeleaf.runner.database-job": JOB,
            "codeleaf.runner.created-at": "1793600000",
        },
    }


def test_network_gateway_accepts_only_internal_job_networks():
    assert validate_network_create(network_body())
    body = network_body()
    body["Internal"] = False
    assert not validate_network_create(body)


def test_network_gateway_rejects_mismatched_job_label():
    body = network_body()
    body["Labels"]["codeleaf.runner.database-job"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert not validate_network_create(body)


def test_gateway_accepts_fixed_mysql_container_template():
    assert validate_database_container_config(
        f"codeleaf-db-mysql-{SUFFIX}", mysql_config(), network_inspect(),
        {"mysql": "mysql:8.4.13", "postgresql": "postgres:17.11-bookworm"},
    )


def test_gateway_rejects_privileged_or_host_mounted_database_container():
    config = mysql_config()
    config["HostConfig"]["Privileged"] = True
    assert not validate_database_container_config(
        f"codeleaf-db-mysql-{SUFFIX}", config, network_inspect(),
        {"mysql": "mysql:8.4.13", "postgresql": "postgres:17.11-bookworm"},
    )
    config = mysql_config()
    config["HostConfig"]["Binds"] = ["/:/host"]
    assert not validate_database_container_config(
        f"codeleaf-db-mysql-{SUFFIX}", config, network_inspect(),
        {"mysql": "mysql:8.4.13", "postgresql": "postgres:17.11-bookworm"},
    )


def test_gateway_rejects_unapproved_image_and_external_network():
    config = mysql_config()
    config["Image"] = "alpine:latest"
    assert not validate_database_container_config(
        f"codeleaf-db-mysql-{SUFFIX}", config, network_inspect(),
        {"mysql": "mysql:8.4.13", "postgresql": "postgres:17.11-bookworm"},
    )
    config = mysql_config()
    info = network_inspect()
    info["Internal"] = False
    assert not validate_database_container_config(
        f"codeleaf-db-mysql-{SUFFIX}", config, info,
        {"mysql": "mysql:8.4.13", "postgresql": "postgres:17.11-bookworm"},
    )


def test_docker_gateway_rejects_query_on_worker_inspect_route(monkeypatch):
    from fastapi.testclient import TestClient
    from app import docker_gateway

    token = "docker-gateway-test-token-with-at-least-32-characters"
    monkeypatch.setattr(docker_gateway, "TOKEN", token)
    gateway = docker_gateway.app
    gateway.state.docker = object()  # Must not be used for a rejected route.
    with TestClient(gateway) as client:
        response = client.get(
            f"/v{docker_gateway.API_VERSION}/containers/{docker_gateway.WORKER_NAME}/json?all=1",
            headers={"X-Docker-Gateway-Key": token},
        )
    assert response.status_code == 403
