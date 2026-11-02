#!/usr/bin/env python3
"""Run real, harmless hello-world probes against installed Piston runtimes."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SMOKE_TESTS_PATH = ROOT / "config" / "runtime-smoke-tests.json"


def normalize(value: Any) -> str:
    return "".join(character for character in str(value or "").strip().lower() if character.isalnum() or character in "+#.-")


def fetch_json(base_url: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        base_url.rstrip("/") + path,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "CodeLeaf-Runner-Smoke-Test/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            return json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        details = exc.read(2048).decode("utf-8", errors="replace")
        raise RuntimeError(f"Piston returned HTTP {exc.code}: {details[:500]}") from exc
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not communicate with the Piston API") from exc


def resolve_runtime(language: str, runtimes: list[dict[str, Any]]) -> dict[str, Any] | None:
    target = normalize(language)
    for runtime in runtimes:
        names = [runtime.get("language", ""), *runtime.get("aliases", [])]
        if runtime.get("runtime"):
            names.append(runtime["runtime"])
        if target in {normalize(name) for name in names}:
            return runtime
    return None


def run_smoke_test(base_url: str, sample: dict[str, Any], runtime: dict[str, Any]) -> tuple[bool, str]:
    request_body = {
        "language": runtime["language"],
        "version": runtime["version"],
        "files": [{"name": sample["file"], "content": sample["source"], "encoding": "utf8"}],
        "stdin": "",
        "args": [],
        # Keep requests within docker-compose.yml defaults. Longer compile
        # allowance is required by JVM languages such as Kotlin and Scala.
        "compile_timeout": 30000,
        "run_timeout": 3000,
        "compile_cpu_time": 30000,
        "run_cpu_time": 3000,
        "compile_memory_limit": 536870912,
        "run_memory_limit": 134217728,
    }
    result = fetch_json(base_url, "/api/v2/execute", request_body, timeout=45.0)
    if not isinstance(result, dict):
        return False, "invalid execution response"
    compile_stage = result.get("compile") or {}
    run_stage = result.get("run") or {}
    if compile_stage and (
        compile_stage.get("code") not in (0, None)
        or compile_stage.get("signal")
        or compile_stage.get("status")
    ):
        detail = compile_stage.get("output") or compile_stage.get("stderr") or compile_stage.get("message") or "compile stage failed"
        return False, str(detail).strip()[:300]
    if run_stage.get("code") != 0 or run_stage.get("signal") or run_stage.get("status"):
        detail = run_stage.get("output") or run_stage.get("stderr") or run_stage.get("message") or "run stage failed"
        return False, str(detail).strip()[:300]
    output = str(run_stage.get("stdout") or run_stage.get("output") or "")
    if sample["expected"] not in output:
        return False, f"expected marker not found; output was {output[:250]!r}"
    return True, output.strip()[:160]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute real smoke tests on installed Piston runtimes.")
    parser.add_argument("--base-url", default=None, help="Piston admin URL (default: PISTON_ADMIN_URL or localhost:2000).")
    parser.add_argument("--require-profile", choices=["core"], default=None, help="Fail if any core runtime is not installed.")
    parser.add_argument("--json", action="store_true", help="Print a JSON report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = (args.base_url or os.getenv("PISTON_ADMIN_URL") or "http://127.0.0.1:2000").rstrip("/")
    try:
        samples = json.loads(SMOKE_TESTS_PATH.read_text(encoding="utf-8"))
        runtimes = fetch_json(base_url, "/api/v2/runtimes")
        if not isinstance(samples, list) or not isinstance(runtimes, list):
            raise RuntimeError("Smoke-test samples or Piston runtime catalogue has an invalid shape")
        report = []
        required_missing = []
        for sample in samples:
            runtime = resolve_runtime(sample["language"], runtimes)
            if runtime is None and sample["language"] == "javascript":
                runtime = resolve_runtime("js", runtimes)
            if runtime is None:
                report.append({"key": sample["key"], "status": "skipped", "reason": "runtime not installed"})
                required_missing.append(sample["key"])
                continue
            try:
                success, detail = run_smoke_test(base_url, sample, runtime)
                report.append({"key": sample["key"], "status": "passed" if success else "failed", "version": runtime.get("version"), "detail": detail})
            except RuntimeError as exc:
                report.append({"key": sample["key"], "status": "failed", "version": runtime.get("version"), "detail": str(exc)})
        passed = sum(1 for item in report if item["status"] == "passed")
        failed = sum(1 for item in report if item["status"] == "failed")
        skipped = sum(1 for item in report if item["status"] == "skipped")
        if args.json:
            print(json.dumps({"passed": passed, "failed": failed, "skipped": skipped, "tests": report}, indent=2))
        else:
            print("CodeLeaf Runner — live Piston execution smoke tests")
            for item in report:
                version = f" ({item['version']})" if item.get("version") else ""
                print(f"{item['status'].upper():<8} {item['key']}{version}: {item.get('detail', item.get('reason', ''))}")
            print(f"\nPassed: {passed} | Failed: {failed} | Skipped: {skipped}")
        if failed:
            return 1
        if args.require_profile == "core" and required_missing:
            print("Required core runtimes missing: " + ", ".join(required_missing), file=sys.stderr)
            return 1
        return 0
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
