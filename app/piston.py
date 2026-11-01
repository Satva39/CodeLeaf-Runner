from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class PistonUnavailable(RuntimeError):
    pass


class PistonRejected(RuntimeError):
    pass


class PistonClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        timeout = httpx.Timeout(
            connect=settings.piston_connect_timeout_seconds,
            read=settings.piston_http_timeout_seconds,
            write=10.0,
            pool=5.0,
        )
        self.client = httpx.AsyncClient(
            base_url=settings.piston_base_url,
            timeout=timeout,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            headers={"User-Agent": "CodeLeaf-Runner/0.1"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def list_runtimes(self) -> list[dict[str, Any]]:
        try:
            response = await self.client.get("/api/v2/runtimes")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PistonUnavailable("Piston runtime catalogue is unavailable.") from exc
        if not isinstance(payload, list):
            raise PistonUnavailable("Piston returned an invalid runtime catalogue.")
        return [item for item in payload if isinstance(item, dict)]

    async def execute(
        self,
        *,
        language: str,
        version: str,
        files: list[dict[str, str]],
        stdin: str,
        args: list[str],
    ) -> dict[str, Any]:
        payload = {
            "language": language,
            "version": version,
            "files": [
                {"name": item["name"], "content": item["content"], "encoding": "utf8"}
                for item in files
            ],
            "stdin": stdin,
            "args": args,
            "compile_timeout": self.settings.piston_compile_timeout_ms,
            "run_timeout": self.settings.piston_run_timeout_ms,
            "compile_cpu_time": self.settings.piston_compile_cpu_time_ms,
            "run_cpu_time": self.settings.piston_run_cpu_time_ms,
            "compile_memory_limit": self.settings.piston_compile_memory_limit_bytes,
            "run_memory_limit": self.settings.piston_run_memory_limit_bytes,
        }
        try:
            response = await self.client.post("/api/v2/execute", json=payload)
        except httpx.TimeoutException as exc:
            raise PistonUnavailable("Piston execution request timed out.") from exc
        except httpx.HTTPError as exc:
            raise PistonUnavailable("Piston execution engine could not be reached.") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise PistonUnavailable("Piston returned an invalid execution response.") from exc

        if response.status_code >= 400:
            message = data.get("message") if isinstance(data, dict) else None
            safe_message = str(message or "Piston rejected the execution request")[:300]
            raise PistonRejected(safe_message)
        if not isinstance(data, dict) or not isinstance(data.get("run"), dict):
            raise PistonUnavailable("Piston returned an incomplete execution response.")
        return data
