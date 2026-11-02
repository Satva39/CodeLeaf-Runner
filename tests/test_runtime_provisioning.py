from __future__ import annotations

import json
from pathlib import Path

from scripts.provision_piston_runtimes import (
    DEFAULT_MANIFEST,
    build_install_plan,
    load_manifest,
    load_lock,
    write_lock,
)


def sample_catalogue():
    return [
        {"language": "c", "language_version": "10.2.0", "installed": True},
        {"language": "c++", "language_version": "10.2.0", "installed": True},
        {"language": "python", "language_version": "3.9.18", "installed": True},
        {"language": "python", "language_version": "3.12.1", "installed": False},
        {"language": "node", "language_version": "22.1.0", "installed": False},
        {"language": "java", "language_version": "21.0.2", "installed": False},
        {"language": "sqlite3", "language_version": "3.45.0", "installed": True},
        {"language": "brainfuck", "language_version": "2.7.3", "installed": False},
    ]


def test_core_profile_uses_live_catalogue_and_maps_javascript_to_node():
    manifest = load_manifest(DEFAULT_MANIFEST)
    plan, missing = build_install_plan(manifest, sample_catalogue(), "core")
    by_language = {item["language"]: item for item in plan}
    assert by_language["python"]["language_version"] == "3.12.1"
    assert by_language["node"]["display_name"] == "javascript"
    assert by_language["sqlite3"]["already_installed"] is True
    assert "c" in by_language and "c++" in by_language
    assert missing
    assert any("no matching package/version in Piston catalogue" in item for item in missing)


def test_all_profile_installs_latest_version_for_each_live_package_only():
    manifest = load_manifest(DEFAULT_MANIFEST)
    catalogue = sample_catalogue()
    plan, missing = build_install_plan(manifest, catalogue, "all")
    assert missing == []
    versions = {item["language"]: item["language_version"] for item in plan}
    assert versions["python"] == "3.12.1"
    assert versions["brainfuck"] == "2.7.3"
    assert len(plan) == len({item["language"] for item in catalogue})


def test_lock_file_pins_exact_package_version(tmp_path: Path):
    manifest = load_manifest(DEFAULT_MANIFEST)
    plan, _ = build_install_plan(
        manifest, sample_catalogue(), "core", {"python": "3.9.18"}
    )
    python_item = next(item for item in plan if item["language"] == "python")
    assert python_item["language_version"] == "3.9.18"

    path = tmp_path / "runtime-lock.json"
    write_lock(path, plan, "http://127.0.0.1:2000")
    loaded = load_lock(path)
    assert loaded["python"] == "3.9.18"
    assert loaded["node"] == "22.1.0"


def test_runtime_lock_rejects_invalid_shape(tmp_path: Path):
    path = tmp_path / "bad-lock.json"
    path.write_text(json.dumps({"schema_version": 1, "packages": "not-a-list"}), encoding="utf-8")
    try:
        load_lock(path)
    except Exception as exc:
        assert "packages array" in str(exc)
    else:
        raise AssertionError("Invalid lock file was accepted")


def test_write_lock_merges_previous_runtime_pins(tmp_path: Path):
    path = tmp_path / "runtime-lock.json"
    path.write_text(
        json.dumps({"schema_version": 1, "packages": [{"language": "python", "version": "3.11.0"}]}),
        encoding="utf-8",
    )
    write_lock(
        path,
        [{"language": "c++", "language_version": "17.0.0"}],
        "http://127.0.0.1:2000",
    )
    packages = {item["language"]: item["version"] for item in json.loads(path.read_text())["packages"]}
    assert packages == {"python": "3.11.0", "c++": "17.0.0"}


def test_runtime_manifest_maps_every_registered_language():
    registry = json.loads((DEFAULT_MANIFEST.parent / "languages.json").read_text(encoding="utf-8"))
    manifest = load_manifest(DEFAULT_MANIFEST)
    registry_keys = {item["key"] for item in registry}
    assert registry_keys == set(manifest["package_candidates"])
    assert set(manifest["profiles"]["extended"]["languages"]) == registry_keys


def test_core_profile_falls_back_to_gcc_and_dotnet_catalogue_packages():
    manifest = load_manifest(DEFAULT_MANIFEST)
    catalogue = [
        {"language": "gcc", "language_version": "10.2.0", "installed": False},
        {"language": "dotnet", "language_version": "5.0.201", "installed": False},
        {"language": "python", "language_version": "3.12.0", "installed": True},
    ]
    plan, missing = build_install_plan(manifest, catalogue, "core")
    assert any(item["language"] == "gcc" and "c" in item["display_name"] and "cpp" in item["display_name"] for item in plan)
    assert any(item["language"] == "dotnet" and item["display_name"] == "csharp" for item in plan)
    assert not any(item.startswith(("c:", "cpp:", "csharp:")) for item in missing)
