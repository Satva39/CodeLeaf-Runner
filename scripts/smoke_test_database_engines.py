#!/usr/bin/env python3
"""Exercise SQLite, MySQL and PostgreSQL through the private Runner API.

Run from the repository root after starting docker compose. The script obtains the
service token from the local .env through the normal Settings loader and never prints it.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any
from uuid import uuid4

import httpx

from app.config import get_settings

# The rate limiter is intentionally scoped per learner. Give each manual smoke
# test invocation a fresh, valid test owner so repeated diagnostics do not consume
# the same learner's production-style per-minute quota.
OWNER_ID = f"codeleaf-db-smoke-{uuid4().hex}"
TEST_SQL = {
    "sqlite": {
        "setup_sql": (
            "CREATE TABLE smoke_people (id INTEGER PRIMARY KEY, name TEXT);"
            "INSERT INTO smoke_people (id, name) VALUES (1, 'CodeLeaf');"
        ),
        "query": "SELECT id, name FROM smoke_people;",
    },
    "mysql": {
        "setup_sql": (
            "CREATE TABLE smoke_people (id INTEGER PRIMARY KEY, name VARCHAR(40));"
            "INSERT INTO smoke_people (id, name) VALUES (1, 'CodeLeaf');"
        ),
        "query": "SELECT id, name FROM smoke_people;",
    },
    "postgresql": {
        "setup_sql": (
            "CREATE TABLE smoke_people (id INTEGER PRIMARY KEY, name VARCHAR(40));"
            "INSERT INTO smoke_people (id, name) VALUES (1, 'CodeLeaf');"
        ),
        "query": "SELECT id, name FROM smoke_people;",
    },
}


def _check_engine(client: httpx.Client, base_url: str, headers: dict[str, str], engine: str) -> dict[str, Any]:
    submitted = client.post(
        f"{base_url}/v1/database/jobs",
        headers=headers,
        json={"engine": engine, **TEST_SQL[engine]},
    )
    if submitted.status_code != 202:
        raise RuntimeError(f"{engine}: job submission returned HTTP {submitted.status_code}: {submitted.text[:500]}")
    job_id = submitted.json().get("job_id")
    if not isinstance(job_id, str):
        raise RuntimeError(f"{engine}: Runner did not return a job ID.")

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        response = client.get(f"{base_url}/v1/database/jobs/{job_id}", headers=headers)
        response.raise_for_status()
        job = response.json()
        if job.get("status") in {"completed", "failed", "cancelled"}:
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            if job.get("status") != "completed" or result.get("error"):
                raise RuntimeError(f"{engine}: execution failed: {result.get('error') or job.get('status')}")
            rows = result.get("rows") or []
            if rows != [[1, "CodeLeaf"]]:
                raise RuntimeError(f"{engine}: unexpected result rows: {rows!r}")
            return result
        time.sleep(0.5)
    raise TimeoutError(f"{engine}: database job did not finish within 60 seconds.")


def main() -> int:
    settings = get_settings()
    if not settings.service_token_ready:
        print("ERROR: Set CODELEAF_RUNNER_SERVICE_TOKEN in .env (32+ characters).", file=sys.stderr)
        return 2
    base_url = os.getenv("RUNNER_SMOKE_API_URL", "http://127.0.0.1:8080").rstrip("/")
    headers = {
        "X-Runner-Key": settings.token_value,
        "X-CodeLeaf-User": OWNER_ID,
    }
    failures = 0
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            engines_response = client.get(f"{base_url}/v1/database/engines", headers={"X-Runner-Key": settings.token_value})
            engines_response.raise_for_status()
            availability = {item["key"]: item["available"] for item in engines_response.json().get("engines", [])}
            for engine in ("sqlite", "mysql", "postgresql"):
                if not availability.get(engine, False):
                    print(f"SKIPPED {engine}: engine is not reported available by the Runner API")
                    failures += 1
                    continue
                try:
                    result = _check_engine(client, base_url, headers, engine)
                    print(f"PASSED  {engine}: rows={result.get('rows')} execution_ms={result.get('execution_ms')}")
                except Exception as exc:
                    failures += 1
                    print(f"FAILED  {engine}: {type(exc).__name__}: {exc}")
    except httpx.HTTPError as exc:
        print(f"ERROR: Cannot reach CodeLeaf Runner API at {base_url}: {exc}", file=sys.stderr)
        return 2
    print(f"\nDatabase smoke tests: {3 - failures}/3 passed; {failures} failed or unavailable.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
