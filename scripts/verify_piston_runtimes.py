#!/usr/bin/env python3
"""Compare CodeLeaf's language registry with the actually installed Piston runtimes."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "config" / "runtime-manifest.json"
REGISTRY_PATH = ROOT / "config" / "languages.json"


def normalize(value: Any) -> str:
    return "".join(character for character in str(value or "").strip().lower() if character.isalnum() or character in "+#.-")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {path}") from exc


def fetch_runtimes(base_url: str, timeout: float = 20.0) -> list[dict[str, Any]]:
    request = Request(
        base_url.rstrip("/") + "/api/v2/runtimes",
        headers={"Accept": "application/json", "User-Agent": "CodeLeaf-Runner-Runtime-Verifier/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(4 * 1024 * 1024 + 1).decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Piston returned HTTP {exc.code} while listing runtimes") from exc
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not retrieve the Piston runtime catalogue") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Piston runtime catalogue must be a JSON array")
    return [item for item in payload if isinstance(item, dict)]


def choose_languages(registry: list[dict[str, Any]], manifest: dict[str, Any], profile: str) -> set[str]:
    if profile == "all":
        return {str(item.get("key", "")) for item in registry if item.get("key")}
    profile_data = manifest.get("profiles", {}).get(profile)
    if not isinstance(profile_data, dict):
        raise RuntimeError(f"Unknown profile: {profile}")
    return set(str(item) for item in profile_data.get("languages", []))


def find_runtime(entry: dict[str, Any], runtimes: list[dict[str, Any]]) -> dict[str, Any] | None:
    expected = {
        normalize(entry.get("key")),
        normalize(entry.get("piston_language")),
        *(normalize(alias) for alias in entry.get("aliases", [])),
    }
    expected.discard("")
    for runtime in runtimes:
        available = {
            normalize(runtime.get("language")),
            *(normalize(alias) for alias in runtime.get("aliases", [])),
            normalize(runtime.get("runtime")),
        }
        available.discard("")
        if expected.intersection(available):
            return runtime
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify installed Piston runtimes against CodeLeaf's language registry.")
    parser.add_argument("--base-url", default=None, help="Piston admin URL (default: PISTON_ADMIN_URL or localhost:2000).")
    parser.add_argument("--profile", choices=["core", "extended", "all"], default="core")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable JSON report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = (args.base_url or os.getenv("PISTON_ADMIN_URL") or "http://127.0.0.1:2000").rstrip("/")
    try:
        registry = load_json(REGISTRY_PATH)
        manifest = load_json(MANIFEST_PATH)
        if not isinstance(registry, list):
            raise RuntimeError("CodeLeaf language registry must be a JSON array")
        runtimes = fetch_runtimes(base_url)
        selected = choose_languages(registry, manifest, args.profile)
        report = []
        for entry in registry:
            if entry.get("key") not in selected:
                continue
            runtime = find_runtime(entry, runtimes)
            report.append(
                {
                    "key": entry.get("key"),
                    "name": entry.get("name"),
                    "available": runtime is not None,
                    "runtime_language": runtime.get("language") if runtime else None,
                    "runtime_version": runtime.get("version") if runtime else None,
                }
            )
        available_count = sum(1 for item in report if item["available"])
        missing_count = len(report) - available_count
        if args.json:
            print(json.dumps({"profile": args.profile, "installed_runtime_count": len(runtimes), "available_count": available_count, "missing_count": missing_count, "languages": report}, indent=2))
        else:
            print(f"CodeLeaf Runner runtime verification — profile: {args.profile}")
            print(f"Piston runtimes installed: {len(runtimes)}")
            print("\nLanguage                         Status       Installed version")
            print("-" * 75)
            for item in report:
                status = "AVAILABLE" if item["available"] else "MISSING"
                print(f"{item['name']:<32} {status:<12} {item['runtime_version'] or '-'}")
            print("-" * 75)
            print(f"Available: {available_count}/{len(report)} | Missing: {missing_count}")
        return 1 if missing_count else 0
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
