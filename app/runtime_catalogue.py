from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_NORMALIZE_RE = re.compile(r"[^a-z0-9+#._-]+")
_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)(?:[-+]([0-9A-Za-z.-]+))?$")


def normalize_package_name(value: Any) -> str:
    return _NORMALIZE_RE.sub("", str(value or "").strip().lower())


def version_sort_key(value: str) -> tuple[Any, ...]:
    """Sort numeric dotted versions naturally; unknown versions sort below semver."""
    text = str(value or "").strip()
    match = _VERSION_RE.fullmatch(text)
    if not match:
        return (0, text.casefold())
    numbers = tuple(int(part) for part in match.group(1).split("."))
    # A stable release sorts above a prerelease with the same numeric version.
    is_stable = 1 if not match.group(2) else 0
    prerelease = match.group(2) or ""
    return (1, numbers, is_stable, prerelease.casefold())


def validate_package_catalogue(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("Piston package catalogue must be a JSON array.")
    packages: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        language = str(item.get("language") or "").strip()
        version = str(item.get("language_version") or item.get("version") or "").strip()
        if not language or not version:
            continue
        packages.append(
            {
                "language": language,
                "language_version": version,
                "installed": bool(item.get("installed", False)),
            }
        )
    return packages


def available_versions(
    packages: Iterable[dict[str, Any]], language_candidates: Iterable[str]
) -> list[dict[str, Any]]:
    candidate_names = {normalize_package_name(name) for name in language_candidates}
    matches = [
        item
        for item in packages
        if normalize_package_name(item.get("language")) in candidate_names
    ]
    return sorted(
        matches,
        key=lambda item: version_sort_key(str(item.get("language_version", ""))),
        reverse=True,
    )


def resolve_package_version(
    packages: Iterable[dict[str, Any]],
    language_candidates: Iterable[str],
    pinned_version: str | None = None,
) -> dict[str, Any] | None:
    matches = available_versions(packages, language_candidates)
    if pinned_version:
        for item in matches:
            if str(item["language_version"]) == pinned_version:
                return item
        return None
    return matches[0] if matches else None


def installed_runtime_lookup(runtimes: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    lookup: dict[str, list[dict[str, Any]]] = {}
    for runtime in runtimes:
        if not isinstance(runtime, dict):
            continue
        names = [runtime.get("language", ""), *runtime.get("aliases", [])]
        if runtime.get("runtime"):
            names.append(runtime["runtime"])
        for name in names:
            normalized = normalize_package_name(name)
            if normalized:
                lookup.setdefault(normalized, []).append(runtime)
    return lookup
