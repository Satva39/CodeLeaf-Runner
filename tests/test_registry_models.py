from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.jobs import prepare_payload
from app.models import CreateJobRequest
from app.registry import load_registry, resolve_language, runtime_matches


def test_registry_has_entries_and_aliases_resolve() -> None:
    registry = load_registry()
    assert len(registry) >= 70
    assert resolve_language("C++")["key"] == "cpp"
    assert resolve_language("cxx")["piston_language"] == "c++"
    assert resolve_language("Py")["key"] == "python"
    assert resolve_language("nodejs")["key"] == "javascript"
    assert resolve_language("sqlite3")["key"] == "sqlite"


def test_registry_language_is_not_automatically_available() -> None:
    entry = resolve_language("cpp")
    assert runtime_matches(entry, []) is None
    runtime = {"language": "c++", "version": "17.0.0", "aliases": ["cpp"]}
    assert runtime_matches(entry, [runtime]) == runtime


def test_create_job_accepts_legacy_single_source_contract() -> None:
    request = CreateJobRequest(
        owner_id="learner-00001234",
        language="C++",
        source="#include <iostream>\nint main(){std::cout << \"hello\";} ",
    )
    entry, payload = prepare_payload(request, Settings())
    assert entry["key"] == "cpp"
    assert payload["piston_language"] == "c++"
    assert payload["files"][0]["name"] == "main.cpp"


def test_create_job_accepts_multiple_root_files() -> None:
    request = CreateJobRequest(
        language="java",
        files=[
            {"name": "Main.java", "content": "class Main {}"},
            {"name": "Helper.java", "content": "class Helper {}"},
        ],
    )
    _, payload = prepare_payload(request, Settings())
    assert len(payload["files"]) == 2


@pytest.mark.parametrize("name", ["../main.cpp", "folder/main.cpp", "..\\main.cpp", "main..cpp", ""])
def test_code_files_reject_path_traversal_and_unsafe_names(name: str) -> None:
    with pytest.raises(ValidationError):
        CreateJobRequest(language="cpp", files=[{"name": name, "content": "int main(){}"}])


def test_create_job_rejects_empty_or_ambiguous_source() -> None:
    with pytest.raises(ValidationError):
        CreateJobRequest(language="python", source="")
    with pytest.raises(ValidationError):
        CreateJobRequest(language="python", source="print(1)", files=[{"name": "main.py", "content": "print(1)"}])


def test_create_job_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CreateJobRequest(language="python", source="print(1)", shell_command="python main.py")


def test_source_size_limit_is_enforced() -> None:
    request = CreateJobRequest(language="python", source="x" * 2048)
    with pytest.raises(Exception):
        prepare_payload(request, Settings(max_file_bytes=1024, max_total_source_bytes=1024))


def test_runner_environment_variables_use_documented_prefix(monkeypatch) -> None:
    monkeypatch.setenv("RUNNER_MAX_QUEUE_DEPTH", "77")
    monkeypatch.setenv("RUNNER_MAX_ACTIVE_PER_USER", "3")
    monkeypatch.setenv("RUNNER_MAX_JOBS_PER_MINUTE", "11")
    monkeypatch.setenv("RUNNER_WORKER_CONCURRENCY", "4")
    monkeypatch.setenv("RUNNER_JOB_TTL_SECONDS", "1800")
    settings = Settings()
    assert settings.max_queue_depth == 77
    assert settings.max_active_per_user == 3
    assert settings.max_jobs_per_minute == 11
    assert settings.worker_concurrency == 4
    assert settings.job_ttl_seconds == 1800


def test_registry_maps_c_cpp_and_csharp_package_aliases():
    assert resolve_language("gcc")["key"] == "c"
    assert resolve_language("g++")["key"] == "cpp"
    assert resolve_language("dotnet")["key"] == "csharp"


def test_create_job_supports_expanded_multi_file_limits():
    request = CreateJobRequest(
        language="python",
        files=[{"name": f"module_{index}.py", "content": f"VALUE_{index} = {index}"} for index in range(1, 26)],
    )
    _, payload = prepare_payload(request, Settings())
    assert len(payload["files"]) == 25
    with pytest.raises(ValidationError):
        CreateJobRequest(
            language="python",
            files=[{"name": f"module_{index}.py", "content": "x"} for index in range(1, 27)],
        )
