from __future__ import annotations

from scripts.smoke_test_piston_runtimes import resolve_runtime, run_smoke_test


def test_resolve_runtime_matches_javascript_aliases():
    runtimes = [
        {"language": "javascript", "version": "22.1.0", "aliases": ["js", "node-js"], "runtime": "node"}
    ]
    assert resolve_runtime("javascript", runtimes) == runtimes[0]
    assert resolve_runtime("js", runtimes) == runtimes[0]


def test_smoke_probe_checks_real_exit_and_expected_output(monkeypatch):
    import scripts.smoke_test_piston_runtimes as smoke

    monkeypatch.setattr(
        smoke,
        "fetch_json",
        lambda *_args, **_kwargs: {"run": {"code": 0, "signal": None, "stdout": "CODELEAF_RUNTIME_OK\n"}},
    )
    sample = {"file": "main.py", "source": "print('CODELEAF_RUNTIME_OK')", "expected": "CODELEAF_RUNTIME_OK"}
    ok, detail = run_smoke_test("http://localhost:2000", sample, {"language": "python", "version": "3.12.0"})
    assert ok is True
    assert "CODELEAF_RUNTIME_OK" in detail


def test_smoke_probe_rejects_compiler_error(monkeypatch):
    import scripts.smoke_test_piston_runtimes as smoke

    monkeypatch.setattr(
        smoke,
        "fetch_json",
        lambda *_args, **_kwargs: {
            "compile": {"code": 1, "stderr": "syntax error", "output": "syntax error"},
            "run": {"code": None, "stdout": ""},
        },
    )
    sample = {"file": "main.cpp", "source": "bad code", "expected": "CODELEAF_RUNTIME_OK"}
    ok, detail = run_smoke_test("http://localhost:2000", sample, {"language": "c++", "version": "17.0.0"})
    assert ok is False
    assert "syntax error" in detail


def test_smoke_probe_stays_within_default_piston_limits(monkeypatch):
    import scripts.smoke_test_piston_runtimes as smoke

    captured = {}

    def fake_fetch_json(_base_url, _path, payload, **_kwargs):
        captured.update(payload)
        return {"run": {"code": 0, "signal": None, "stdout": "WRONG_OUTPUT\n"}}

    monkeypatch.setattr(smoke, "fetch_json", fake_fetch_json)
    sample = {"file": "main.cpp", "source": "int main() { return 0; }", "expected": "CODELEAF_RUNTIME_OK"}
    # The fake response intentionally omits the marker, so the probe returns
    # false after we have captured the request. The timeout contract is what
    # this test verifies.
    ok, _detail = smoke.run_smoke_test(
        "http://localhost:2000",
        sample,
        {"language": "c++", "version": "17.0.0"},
    )
    assert ok is False
    assert captured["compile_timeout"] == 30000
    assert captured["compile_cpu_time"] == 30000
    assert captured["run_timeout"] == 3000
    assert captured["run_cpu_time"] == 3000
