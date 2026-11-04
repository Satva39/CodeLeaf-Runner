from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote

import httpx


class DockerEngineError(RuntimeError):
    pass


class DockerEngine:
    """Small allow-listed Docker Engine API client over the local Unix socket.

    This client is used only by the private database-worker process. It never accepts
    user-provided image names, commands, mounts, networks, or container options.
    """

    def __init__(
        self, socket_path: str, api_version: str = "1.45", gateway_token: str = ""
    ) -> None:
        headers = {"X-Docker-Gateway-Key": gateway_token} if gateway_token else {}
        if socket_path.startswith(("http://", "https://")):
            self.client = httpx.AsyncClient(
                base_url=socket_path.rstrip("/"), headers=headers,
                timeout=httpx.Timeout(connect=4.0, read=30.0, write=10.0, pool=5.0),
                limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
            )
        else:
            transport = httpx.AsyncHTTPTransport(uds=socket_path, retries=0)
            self.client = httpx.AsyncClient(
                transport=transport,
                base_url="http://docker",
                headers=headers,
                timeout=httpx.Timeout(connect=4.0, read=30.0, write=10.0, pool=5.0),
            )
        self.prefix = f"/v{api_version}"

    async def close(self) -> None:
        await self.client.aclose()

    async def verify_host(self, worker_container_name: str) -> None:
        try:
            ping = await self.client.get("/_ping")
        except httpx.HTTPError as exc:
            raise DockerEngineError("Database worker cannot reach the Docker Engine socket.") from exc
        if ping.status_code != 200:
            raise DockerEngineError("Docker Engine health check failed.")
        try:
            worker = await self.client.get(
                f"{self.prefix}/containers/{quote(worker_container_name, safe='')}/json"
            )
        except httpx.HTTPError as exc:
            raise DockerEngineError("Database worker container cannot be inspected.") from exc
        if worker.status_code != 200:
            raise DockerEngineError(
                "The configured database-worker container is not visible to Docker. "
                "Check RUNNER_DATABASE_WORKER_CONTAINER and the Compose container_name."
            )

    async def create_internal_network(self, job_id: str) -> tuple[str, str]:
        suffix = job_id.replace("-", "")[:20]
        name = f"codeleaf-db-net-{suffix}"
        response = await self.client.post(
            f"{self.prefix}/networks/create",
            json={
                "Name": name,
                "Driver": "bridge",
                "Internal": True,
                "CheckDuplicate": True,
                "Labels": {
                    "codeleaf.runner.managed": "true",
                    "codeleaf.runner.database-job": job_id,
                    "codeleaf.runner.created-at": str(int(time.time())),
                },
            },
        )
        if response.status_code >= 400:
            detail = " ".join(response.text.split())[:600]
            raise DockerEngineError(
                f"Docker could not create the private database network "
                f"(HTTP {response.status_code}): "
                f"{detail or 'No response details'}"
            )
        data = response.json()
        network_id = data.get("Id") if isinstance(data, dict) else None
        if not isinstance(network_id, str) or len(network_id) < 12:
            raise DockerEngineError("Docker returned an invalid database network identifier.")
        return network_id, name

    async def connect_worker_to_network(self, network_id: str, worker_container_name: str) -> None:
        worker = await self.client.get(
            f"{self.prefix}/containers/{quote(worker_container_name, safe='')}/json"
        )
        if worker.status_code != 200:
            raise DockerEngineError("Docker could not inspect the database-worker container.")
        worker_id = worker.json().get("Id")
        if not isinstance(worker_id, str) or len(worker_id) < 12:
            raise DockerEngineError("Docker returned an invalid database-worker identifier.")
        response = await self.client.post(
            f"{self.prefix}/networks/{network_id}/connect",
            json={"Container": worker_id},
        )
        if response.status_code not in {200, 201}:
            raise DockerEngineError("Docker could not attach the database worker to the private job network.")

    async def disconnect_worker_from_network(self, network_id: str, worker_container_name: str) -> None:
        worker = await self.client.get(
            f"{self.prefix}/containers/{quote(worker_container_name, safe='')}/json"
        )
        if worker.status_code == 404:
            return
        if worker.status_code != 200:
            raise DockerEngineError("Docker could not inspect the database-worker container during cleanup.")
        worker_id = worker.json().get("Id")
        if not isinstance(worker_id, str):
            return
        response = await self.client.post(
            f"{self.prefix}/networks/{network_id}/disconnect",
            json={"Container": worker_id, "Force": True},
        )
        if response.status_code not in {200, 204, 404}:
            raise DockerEngineError("Docker could not detach the worker from the private database network.")

    async def list_managed_database_networks(self) -> list[dict[str, Any]]:
        filters = json.dumps({"label": ["codeleaf.runner.managed=true"]}, separators=(",", ":"))
        response = await self.client.get(
            f"{self.prefix}/networks", params={"filters": filters}
        )
        if response.status_code >= 400:
            raise DockerEngineError("Docker could not list managed database networks.")
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    async def remove_internal_network(self, network_id: str) -> None:
        try:
            response = await self.client.delete(f"{self.prefix}/networks/{network_id}")
        except httpx.HTTPError as exc:
            raise DockerEngineError("Docker could not remove the private database network.") from exc
        if response.status_code not in {204, 404}:
            raise DockerEngineError("Docker could not remove the private database network.")

    async def remove_ephemeral_database(
        self, container_id: str | None, network_id: str | None, worker_container_name: str
    ) -> None:
        if container_id:
            await self.remove_container(container_id)
        if network_id:
            await self.disconnect_worker_from_network(network_id, worker_container_name)
            await self.remove_internal_network(network_id)

    async def ensure_image(self, image: str) -> None:
        path = f"{self.prefix}/images/{quote(image, safe=':')}/json"
        response = await self.client.get(path)
        if response.status_code == 200:
            return
        if response.status_code != 404:
            raise DockerEngineError(f"Docker image inspection failed: HTTP {response.status_code}.")
        try:
            pull = await self.client.post(
                f"{self.prefix}/images/create", params={"fromImage": image}, timeout=300.0
            )
        except httpx.HTTPError as exc:
            raise DockerEngineError("Docker could not pull the configured database image.") from exc
        if pull.status_code >= 400:
            raise DockerEngineError("Docker rejected the database image pull.")
        for line in pull.text.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("error") or event.get("errorDetail"):
                raise DockerEngineError("Docker reported an error while pulling the database image.")

    async def create_container(self, *, name: str, config: dict[str, Any]) -> str:
        try:
            response = await self.client.post(
                f"{self.prefix}/containers/create",
                params={"name": name},
                json=config,
            )
        except httpx.HTTPError as exc:
            raise DockerEngineError("Docker could not create the database sandbox.") from exc
        if response.status_code >= 400:
            raise DockerEngineError(f"Docker rejected database sandbox creation (HTTP {response.status_code}).")
        data = response.json()
        container_id = data.get("Id")
        if not isinstance(container_id, str) or len(container_id) < 12:
            raise DockerEngineError("Docker returned an invalid database sandbox identifier.")
        return container_id

    async def start_container(self, container_id: str) -> None:
        response = await self.client.post(f"{self.prefix}/containers/{container_id}/start")
        if response.status_code not in {204, 304}:
            raise DockerEngineError(f"Docker could not start the database sandbox (HTTP {response.status_code}).")

    async def inspect_container(self, container_id: str) -> dict[str, Any]:
        response = await self.client.get(f"{self.prefix}/containers/{container_id}/json")
        if response.status_code >= 400:
            raise DockerEngineError("Docker could not inspect the database sandbox.")
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def list_managed_database_containers(self) -> list[dict[str, Any]]:
        filters = json.dumps({"label": ["codeleaf.runner.managed=true"]}, separators=(",", ":"))
        response = await self.client.get(
            f"{self.prefix}/containers/json", params={"all": "1", "filters": filters}
        )
        if response.status_code >= 400:
            raise DockerEngineError("Docker could not list managed database sandboxes.")
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    async def remove_container(self, container_id: str) -> None:
        try:
            response = await self.client.delete(
                f"{self.prefix}/containers/{container_id}",
                params={"force": "true", "v": "true"},
            )
        except httpx.HTTPError as exc:
            raise DockerEngineError("Docker could not remove the database sandbox.") from exc
        if response.status_code not in {204, 404}:
            raise DockerEngineError("Docker could not remove the database sandbox.")

    async def create_ephemeral_database(
        self,
        *,
        engine: str,
        job_id: str,
        image: str,
        worker_container_name: str,
        root_password: str,
        database_user: str,
        database_password: str,
    ) -> tuple[str, str, str]:
        await self.ensure_image(image)
        suffix = job_id.replace("-", "")[:20]
        name = f"codeleaf-db-{engine}-{suffix}"
        network_id: str | None = None
        container_id: str | None = None
        try:
            network_id, network_name = await self.create_internal_network(job_id)
            await self.connect_worker_to_network(network_id, worker_container_name)
            if engine == "postgresql":
                env = [
                    f"POSTGRES_USER=admin_{suffix}",
                    f"POSTGRES_PASSWORD={root_password}",
                    "POSTGRES_DB=codeleaf",
                    "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256",
                    "PGDATA=/var/lib/postgresql/data/pgdata",
                ]
                command = [
                    "postgres", "-c", "listen_addresses=*", "-c", "max_connections=12",
                    "-c", "statement_timeout=3000", "-c", "lock_timeout=750",
                    "-c", "idle_in_transaction_session_timeout=3000", "-c", "temp_file_limit=32MB",
                    "-c", "work_mem=4MB", "-c", "max_parallel_workers_per_gather=0",
                    "-c", "log_destination=stderr",
                ]
                tmpfs = {
                    "/var/lib/postgresql/data": "rw,nosuid,nodev,noexec,size=256m",
                    "/var/run/postgresql": "rw,nosuid,nodev,noexec,size=16m",
                    "/tmp": "rw,nosuid,nodev,noexec,size=32m",
                }
            elif engine == "mysql":
                env = [
                    f"MYSQL_ROOT_PASSWORD={root_password}", "MYSQL_DATABASE=codeleaf",
                    f"MYSQL_USER={database_user}", f"MYSQL_PASSWORD={database_password}",
                ]
                command = [
                    "--bind-address=0.0.0.0", "--max-connections=12", "--max-execution-time=3000",
                    "--local-infile=0", "--secure-file-priv=/var/lib/mysql-files",
                    "--performance-schema=OFF", "--skip-log-bin", "--max-allowed-packet=1M",
                    "--innodb-buffer-pool-size=64M", "--innodb-log-buffer-size=8M", "--tmpdir=/tmp",
                ]
                tmpfs = {
                    "/var/lib/mysql": "rw,nosuid,nodev,noexec,size=256m",
                    "/var/lib/mysql-files": "rw,nosuid,nodev,noexec,size=8m",
                    "/var/run/mysqld": "rw,nosuid,nodev,noexec,size=16m",
                    "/tmp": "rw,nosuid,nodev,noexec,size=32m",
                }
            else:
                raise DockerEngineError("Unsupported disposable database engine.")

            config = {
                "Image": image,
                "Env": env,
                "Cmd": command,
                "Labels": {
                    "codeleaf.runner.managed": "true",
                    "codeleaf.runner.database-job": job_id,
                    "codeleaf.runner.database-engine": engine,
                    "codeleaf.runner.database-network-id": network_id,
                },
                "HostConfig": {
                    "NetworkMode": network_name,
                    "Memory": 536870912,
                    "MemorySwap": 536870912,
                    "NanoCpus": 500000000,
                    "PidsLimit": 64,
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "CapAdd": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "Tmpfs": tmpfs,
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
            # Retry recovery: a previous attempt may have left a same-job container.
            try:
                existing = await self.client.get(f"{self.prefix}/containers/{quote(name, safe='')}/json")
                if existing.status_code == 200:
                    existing_id = existing.json().get("Id")
                    if isinstance(existing_id, str) and existing_id:
                        await self.remove_container(existing_id)
            except (httpx.HTTPError, ValueError):
                pass
            container_id = await self.create_container(name=name, config=config)
            await self.start_container(container_id)
            return container_id, name, network_id
        except Exception:
            if container_id:
                try:
                    await self.remove_container(container_id)
                except DockerEngineError:
                    pass
            if network_id:
                try:
                    await self.disconnect_worker_from_network(network_id, worker_container_name)
                    await self.remove_internal_network(network_id)
                except DockerEngineError:
                    pass
            raise
