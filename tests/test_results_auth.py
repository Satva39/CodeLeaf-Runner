from __future__ import annotations

from app.auth import valid_service_key
from app.results import normalize_piston_result


SECRET = "phase-one-test-secret-value-that-is-at-least-32-characters"


def test_service_key_uses_constant_time_comparison_contract() -> None:
    assert valid_service_key(SECRET, SECRET)
    assert not valid_service_key("wrong", SECRET)
    assert not valid_service_key(SECRET, "short")
    assert not valid_service_key("", SECRET)


def test_normalize_successful_compiled_program() -> None:
    result = normalize_piston_result(
        {
            "language": "c++",
            "version": "17.0.0",
            "compile": {
                "stdout": "",
                "stderr": "",
                "output": "",
                "code": 0,
                "status": None,
                "wall_time": 120,
                "cpu_time": 80,
                "memory": 8_000_000,
            },
            "run": {
                "stdout": "Hello CodeLeaf\n",
                "stderr": "",
                "output": "Hello CodeLeaf\n",
                "code": 0,
                "status": None,
                "wall_time": 3,
                "cpu_time": 2,
                "memory": 2_000_000,
            },
        },
        150,
    )
    assert result["provider"] == "codeleaf-runner"
    assert result["outcome"] == "success"
    assert result["stdout"] == "Hello CodeLeaf\n"
    assert result["exit_code"] == 0
    assert result["memory_bytes"] == 8_000_000
    assert result["duration_ms"] == 150


def test_normalize_compile_error_into_stderr() -> None:
    result = normalize_piston_result(
        {
            "language": "c++",
            "version": "17.0.0",
            "compile": {
                "stdout": "",
                "stderr": "main.cpp:3: error: expected ';'\n",
                "output": "main.cpp:3: error: expected ';'\n",
                "code": 1,
                "status": "RE",
                "wall_time": 20,
                "cpu_time": 18,
                "memory": 1000,
            },
            "run": {"stdout": "", "stderr": "", "output": "", "code": None, "signal": None},
        },
        30,
    )
    assert result["outcome"] == "compile_error"
    assert "expected" in result["stderr"]


def test_normalize_timeout_status() -> None:
    result = normalize_piston_result(
        {"run": {"stdout": "", "stderr": "", "output": "", "code": None, "status": "TO"}},
        3100,
    )
    assert result["outcome"] == "timeout"


def test_compile_signal_without_exit_code_is_not_reported_as_success() -> None:
    result = normalize_piston_result(
        {
            "compile": {"stdout": "", "stderr": "killed", "code": None, "signal": "SIGKILL", "status": "SG"},
            "run": {"stdout": "", "stderr": "", "code": None, "signal": None, "status": None},
        },
        50,
    )
    assert result["outcome"] == "compile_error"


def test_deployment_template_secret_is_not_accepted_as_a_valid_service_token() -> None:
    from pathlib import Path

    env_template = Path(__file__).resolve().parents[1] / ".env.example"
    line = next(
        item for item in env_template.read_text(encoding="utf-8").splitlines()
        if item.startswith("CODELEAF_RUNNER_SERVICE_TOKEN=")
    )
    token = line.split("=", 1)[1]
    assert not valid_service_key(token, token)
