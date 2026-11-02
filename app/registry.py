from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import HTTPException

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "config" / "languages.json"
NORMALIZE_RE = re.compile(r"[^a-z0-9+#.-]+")


def normalize_language(value: str) -> str:
    return NORMALIZE_RE.sub("", str(value).strip().lower())


@lru_cache(maxsize=1)
def load_registry() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("CodeLeaf language registry could not be loaded") from exc
    if not isinstance(raw, list):
        raise RuntimeError("CodeLeaf language registry must be a JSON list")

    registry: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid language registry entry")
        key = str(item.get("key", "")).strip().lower()
        piston_language = str(item.get("piston_language", "")).strip().lower()
        if not key or not piston_language or key in registry:
            raise RuntimeError("Language registry contains a missing or duplicate key")

        aliases: list[str] = []
        local_seen: set[str] = set()
        for alias in [key, piston_language, *item.get("aliases", [])]:
            normalized = normalize_language(alias)
            if not normalized:
                raise RuntimeError(f"Invalid language alias: {alias}")
            if normalized in local_seen:
                continue
            if normalized in seen:
                raise RuntimeError(f"Language alias belongs to multiple entries: {alias}")
            local_seen.add(normalized)
            seen.add(normalized)
            aliases.append(str(alias))
        entry = dict(item)
        entry["aliases"] = aliases
        registry[key] = entry
    return registry


def resolve_language(value: str) -> dict[str, Any]:
    normalized = normalize_language(value)
    for entry in load_registry().values():
        if normalized in {normalize_language(alias) for alias in entry["aliases"]}:
            return entry
    raise HTTPException(status_code=400, detail="This language is not in the CodeLeaf Runner registry.")


def runtime_matches(entry: dict[str, Any], runtimes: list[dict[str, Any]]) -> dict[str, Any] | None:
    wanted = {normalize_language(alias) for alias in entry["aliases"]}
    wanted.add(normalize_language(entry["piston_language"]))
    for runtime in runtimes:
        if not isinstance(runtime, dict):
            continue
        names = [runtime.get("language", ""), *runtime.get("aliases", [])]
        if any(normalize_language(name) in wanted for name in names):
            return runtime
    return None


def public_language(entry: dict[str, Any], runtime: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "key": entry["key"],
        "name": entry["name"],
        "category": entry["category"],
        "extension": entry["extension"],
        "piston_language": entry["piston_language"],
        "available": runtime is not None,
        "installed_version": runtime.get("version") if runtime else None,
        "runtime_language": runtime.get("language") if runtime else None,
        "runtime_kind": runtime.get("runtime") if runtime else None,
        "aliases": entry["aliases"],
    }
