from __future__ import annotations

from typing import Any


def _stage_value(stage: dict[str, Any], key: str, default: Any = None) -> Any:
    value = stage.get(key, default)
    if isinstance(value, str):
        return value[:16384]
    return value


def normalize_piston_result(payload: dict[str, Any], duration_ms: int) -> dict[str, Any]:
    compile_stage = payload.get("compile") if isinstance(payload.get("compile"), dict) else {}
    run_stage = payload.get("run") if isinstance(payload.get("run"), dict) else {}

    compile_stdout = _stage_value(compile_stage, "stdout", "")
    compile_stderr = _stage_value(compile_stage, "stderr", "")
    run_stdout = _stage_value(run_stage, "stdout", "")
    run_stderr = _stage_value(run_stage, "stderr", "")
    stdout = (compile_stdout + run_stdout)[-32768:]
    stderr = (compile_stderr + run_stderr)[-32768:]

    compile_status = compile_stage.get("status")
    run_status = run_stage.get("status")
    compile_code = compile_stage.get("code")
    run_code = run_stage.get("code")

    error_statuses = {"RE", "SG", "OL", "EL", "XX"}
    if compile_status == "TO" or run_status == "TO":
        outcome = "timeout"
    elif compile_stage and (compile_code not in (None, 0) or compile_status in error_statuses):
        outcome = "compile_error"
    elif run_code not in (None, 0) or run_status in error_statuses:
        outcome = "runtime_error"
    else:
        outcome = "success"

    compile_wall = compile_stage.get("wall_time", 0) or 0
    run_wall = run_stage.get("wall_time", 0) or 0
    compile_cpu = compile_stage.get("cpu_time", 0) or 0
    run_cpu = run_stage.get("cpu_time", 0) or 0
    memory_values = [
        value for value in (compile_stage.get("memory"), run_stage.get("memory"))
        if isinstance(value, (int, float))
    ]

    combined_output_parts = []
    if compile_stage.get("output"):
        combined_output_parts.append(str(compile_stage["output"]))
    if run_stage.get("output"):
        combined_output_parts.append(str(run_stage["output"]))
    combined_output = "".join(combined_output_parts)[-32768:]

    return {
        "provider": "codeleaf-runner",
        "engine": "piston",
        "outcome": outcome,
        "stdout": stdout,
        "stderr": stderr,
        "output": combined_output or (stdout + stderr),
        "compile_stdout": compile_stdout,
        "compile_stderr": compile_stderr,
        "run_stdout": run_stdout,
        "run_stderr": run_stderr,
        "compile_exit_code": compile_code,
        "exit_code": run_code,
        "compile_status": compile_status,
        "run_status": run_status,
        "compile_signal": compile_stage.get("signal"),
        "run_signal": run_stage.get("signal"),
        "compile_message": _stage_value(compile_stage, "message"),
        "run_message": _stage_value(run_stage, "message"),
        "runtime_language": payload.get("language"),
        "runtime_version": payload.get("version"),
        "compile_time_ms": compile_wall,
        "run_time_ms": run_wall,
        "cpu_time_ms": compile_cpu + run_cpu,
        "memory_bytes": max(memory_values) if memory_values else None,
        "duration_ms": max(0, int(duration_ms)),
    }
