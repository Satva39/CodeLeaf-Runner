#!/usr/bin/env python3
"""Provision Piston runtime packages from its live package catalogue.

Safe by default: the script only prints a plan unless --apply is supplied.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.runtime_catalogue import resolve_package_version, validate_package_catalogue, version_sort_key

DEFAULT_MANIFEST = ROOT / "config" / "runtime-manifest.json"
DEFAULT_LOCK = ROOT / "config" / "runtime-lock.json"
USER_AGENT = "CodeLeaf-Runner-Runtime-Provisioner/1.0"


class PistonAdminError(RuntimeError):
    pass


class PistonAdminClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
        except HTTPError as exc:
            body_text = exc.read(2048).decode("utf-8", errors="replace")
            raise PistonAdminError(
                f"Piston returned HTTP {exc.code} for {method} {path}: {body_text[:500]}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise PistonAdminError(
                f"Cannot reach Piston at {self.base_url}. Check that the Piston container is running."
            ) from exc
        if len(raw) > 8 * 1024 * 1024:
            raise PistonAdminError(f"Piston response from {path} exceeded the 8 MiB safety limit.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PistonAdminError(f"Piston returned invalid JSON for {method} {path}.") from exc

    def list_packages(self) -> Any:
        return self.request_json("GET", "/api/v2/packages")

    def list_runtimes(self) -> Any:
        return self.request_json("GET", "/api/v2/runtimes")

    def install(self, language: str, version: str) -> Any:
        # Runtime package downloads can be large, so use a generous timeout only for this request.
        old_timeout = self.timeout
        self.timeout = max(old_timeout, 1800.0)
        try:
            return self.request_json(
                "POST", "/api/v2/packages", {"language": language, "version": version}
            )
        finally:
            self.timeout = old_timeout


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PistonAdminError(f"Could not read runtime manifest: {path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise PistonAdminError("Runtime manifest has an unsupported schema version.")
    if not isinstance(data.get("profiles"), dict) or not isinstance(data.get("package_candidates"), dict):
        raise PistonAdminError("Runtime manifest is missing profiles or package_candidates.")
    return data


def load_lock(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PistonAdminError(f"Could not read runtime lock file: {path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise PistonAdminError("Runtime lock file has an unsupported schema version.")
    packages = data.get("packages")
    if not isinstance(packages, list):
        raise PistonAdminError("Runtime lock file must contain a packages array.")
    result: dict[str, str] = {}
    for item in packages:
        if isinstance(item, dict) and item.get("language") and item.get("version"):
            result[str(item["language"]).casefold()] = str(item["version"])
    return result


def resolve_catalogue_entry(
    catalogue: list[dict[str, Any]], candidates: list[str], pinned_version: str | None
) -> dict[str, Any] | None:
    return resolve_package_version(catalogue, candidates, pinned_version)


def build_install_plan(
    manifest: dict[str, Any],
    catalogue: list[dict[str, Any]],
    profile: str,
    lock_versions: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    lock_versions = lock_versions or {}
    candidates_map: dict[str, list[str]] = manifest["package_candidates"]
    missing: list[str] = []
    plan: list[dict[str, Any]] = []

    if profile == "all":
        # Install the newest version of each package actually advertised by this Piston node.
        package_names = sorted({str(item["language"]) for item in catalogue}, key=str.casefold)
        selections = [(name, [name]) for name in package_names]
    else:
        profile_data = manifest["profiles"].get(profile)
        if not isinstance(profile_data, dict):
            raise PistonAdminError(f"Unknown profile {profile!r}.")
        selections = []
        for language_key in profile_data.get("languages", []):
            candidates = candidates_map.get(language_key)
            if not isinstance(candidates, list) or not candidates:
                missing.append(f"{language_key}: no package mapping in manifest")
                continue
            selections.append((language_key, candidates))

    for display_name, candidates in selections:
        pinned = None
        for candidate in candidates:
            pinned = lock_versions.get(candidate.casefold())
            if pinned:
                break
        match = resolve_catalogue_entry(catalogue, candidates, pinned)
        if match is None:
            missing.append(f"{display_name}: no matching package/version in Piston catalogue")
            continue
        entry = dict(match)
        entry["display_name"] = display_name
        entry["already_installed"] = bool(match.get("installed"))
        plan.append(entry)

    # Multiple registry keys may map to one Piston package. Install each exact package once.
    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for item in plan:
        key = (str(item["language"]).casefold(), str(item["language_version"]))
        if key in deduplicated:
            deduplicated[key]["display_name"] += f", {item['display_name']}"
        else:
            deduplicated[key] = item
    return list(deduplicated.values()), missing


def package_runtime_is_visible(
    package_language: str,
    runtimes: list[dict[str, Any]],
    aliases: dict[str, list[str]],
) -> bool:
    expected = {package_language.casefold()}
    expected.update(
        str(value).casefold()
        for value in aliases.get(package_language, aliases.get(package_language.casefold(), []))
    )
    for runtime in runtimes:
        if not isinstance(runtime, dict):
            continue
        available = {str(runtime.get("language", "")).casefold(), str(runtime.get("runtime", "")).casefold()}
        available.update(str(value).casefold() for value in runtime.get("aliases", []) if value)
        available.discard("")
        if expected.intersection(available):
            return True
    return False


def write_lock(path: Path, installed: list[dict[str, Any]], piston_base_url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    merged: dict[str, str] = {}
    if path.exists():
        for item in load_lock(path).items():
            merged[item[0]] = item[1]
    for item in installed:
        merged[str(item["language"])] = str(item["language_version"])
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "piston_base_url": piston_base_url,
        "packages": [
            {"language": language, "version": version}
            for language, version in sorted(merged.items(), key=lambda row: row[0].casefold())
        ],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install Piston language runtimes using the live Piston package catalogue."
    )
    parser.add_argument("--base-url", default=None, help="Piston admin URL (default: PISTON_ADMIN_URL or localhost:2000).")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--profile", choices=["core", "extended", "all"], default="core")
    parser.add_argument("--lock-file", type=Path, default=None, help="Use exact versions from an existing runtime lock.")
    parser.add_argument("--write-lock", type=Path, default=None, help="Write exact versions confirmed installed after provisioning.")
    parser.add_argument("--apply", action="store_true", help="Actually install packages. Without this flag, only show the plan.")
    parser.add_argument("--confirm-all", action="store_true", help="Required together with --profile all --apply.")
    parser.add_argument("--list-catalogue", action="store_true", help="Print every available Piston package version and exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    import os

    base_url = (args.base_url or os.getenv("PISTON_ADMIN_URL") or "http://127.0.0.1:2000").rstrip("/")
    if args.profile == "all" and args.apply and not args.confirm_all:
        print("Refusing to install every available package without --confirm-all.", file=sys.stderr)
        return 2
    client = PistonAdminClient(base_url)
    try:
        catalogue_raw = client.list_packages()
        if not isinstance(catalogue_raw, list):
            raise PistonAdminError("Piston /api/v2/packages did not return an array.")
        catalogue = validate_package_catalogue(catalogue_raw)
        if args.list_catalogue:
            for item in sorted(catalogue, key=lambda row: (row["language"].casefold(), version_sort_key(row["language_version"]))):
                state = "installed" if item["installed"] else "available"
                print(f"{item['language']:<18} {item['language_version']:<16} {state}")
            return 0

        manifest = load_manifest(args.manifest)
        lock = load_lock(args.lock_file)
        plan, missing = build_install_plan(manifest, catalogue, args.profile, lock)

        print(f"Piston: {base_url}")
        print(f"Profile: {args.profile}")
        print(f"Package catalogue entries: {len(catalogue)}")
        print("\nRuntime installation plan:")
        for item in plan:
            status = "already installed" if item["already_installed"] else ("INSTALL" if args.apply else "would install")
            print(f"  {item['language']:<18} {item['language_version']:<16} {status}  [{item['display_name']}]")
        for message in missing:
            print(f"  MISSING: {message}")
        if not args.apply:
            print("\nDry run only. Add --apply to install these packages.")
            if args.profile == "all":
                print("Installing all packages can require substantial disk space and time.")
            return 1 if missing else 0

        installed_or_confirmed: list[dict[str, Any]] = []
        failed: list[str] = []
        for index, item in enumerate(plan, start=1):
            if item["already_installed"]:
                installed_or_confirmed.append(item)
                continue
            try:
                print(f"\n[{index}/{len(plan)}] Installing {item['language']} {item['language_version']}...", flush=True)
                response = client.install(item["language"], item["language_version"])
                confirmed_version = str(response.get("version", "")) if isinstance(response, dict) else ""
                confirmed_language = str(response.get("language", "")) if isinstance(response, dict) else ""
                if confirmed_language.casefold() != item["language"].casefold() or confirmed_version != item["language_version"]:
                    raise PistonAdminError("Piston installation response did not match the requested package/version.")
                print(f"  Installed {confirmed_language} {confirmed_version}")
                installed_or_confirmed.append(item)
            except PistonAdminError as exc:
                failed.append(f"{item['language']} {item['language_version']}: {exc}")
                print(f"  FAILED: {exc}", file=sys.stderr)

        time.sleep(1)
        runtimes_raw = client.list_runtimes()
        if not isinstance(runtimes_raw, list):
            raise PistonAdminError("Piston /api/v2/runtimes did not return an array after installation.")
        package_runtime_aliases = manifest.get("package_runtime_aliases", {})
        if not isinstance(package_runtime_aliases, dict):
            package_runtime_aliases = {}
        visible_packages: set[str] = set()
        for item in installed_or_confirmed:
            package_language = str(item["language"])
            if package_runtime_is_visible(package_language, runtimes_raw, package_runtime_aliases):
                visible_packages.add(package_language.casefold())
            else:
                failed.append(f"{package_language} {item['language_version']}: absent from installed runtime list")

        if args.write_lock:
            lock_entries = [
                item for item in installed_or_confirmed
                if str(item["language"]).casefold() in visible_packages
            ]
            write_lock(args.write_lock, lock_entries, base_url)
            print(f"\nRuntime lock written: {args.write_lock}")

        print(f"\nInstalled runtime entries reported by Piston: {len(runtimes_raw)}")
        if failed:
            print("\nProvisioning finished with errors:", file=sys.stderr)
            for message in failed:
                print(f"  - {message}", file=sys.stderr)
        if missing:
            print("\nSome CodeLeaf registry languages are not in this Piston package catalogue.", file=sys.stderr)
        return 1 if failed or missing else 0
    except PistonAdminError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
