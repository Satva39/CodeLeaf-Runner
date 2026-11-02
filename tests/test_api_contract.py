from __future__ import annotations

import asyncio
import json

import httpx

from app import auth, main
from app.config import Settings

SECRET = "phase-one-api-test-secret-value-at-least-32-characters"


class FakeRedis:
    def __init__(self):
        self.record = {}
        self.rate = 0

    async def eval(self, *args):
        from app.security import RATE_LIMIT_LUA
        if args and args[0] == RATE_LIMIT_LUA:
            self.rate += 1
            return [1, 60, self.rate]
        return ["ok", "1750000000000-0"]

    async def hgetall(self, _key):
        return self.record


class FakePiston:
    async def list_runtimes(self):
        return [
            {"language": "c++", "version": "17.0.0", "aliases": ["cpp"]},
            {"language": "javascript", "version": "22.0.0", "aliases": ["js"], "runtime": "node"},
        ]

    async def list_packages(self):
        return [
            {"language": "c++", "language_version": "17.0.0", "installed": True},
            {"language": "node", "language_version": "22.0.0", "installed": True},
            {"language": "python", "language_version": "3.12.0", "installed": False},
        ]


def request(method: str, url: str, **kwargs):
    async def execute():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, url, **kwargs)
    return asyncio.run(execute())


def setup_api(monkeypatch):
    settings = Settings(service_token=SECRET)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    main.app.state.redis = FakeRedis()
    main.app.state.piston = FakePiston()


def test_create_job_matches_existing_codeleaf_backend_contract(monkeypatch):
    setup_api(monkeypatch)
    response = request(
        "POST",
        "/v1/jobs",
        headers={"X-Runner-Key": SECRET},
        json={
            "owner_id": "learner-00001234",
            "language": "cpp",
            "source": "#include <iostream>\nint main(){std::cout << 123;} ",
            "stdin": "",
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "queued"
    assert data["job_id"]


def test_create_job_rejects_bad_service_secret(monkeypatch):
    setup_api(monkeypatch)
    response = request(
        "POST",
        "/v1/jobs",
        headers={"X-Runner-Key": "incorrect"},
        json={"owner_id": "learner-00001234", "language": "cpp", "source": "int main(){}"},
    )
    assert response.status_code == 401


def test_get_job_is_owner_scoped(monkeypatch):
    setup_api(monkeypatch)
    main.app.state.redis.record = {
        "job_id": "00000000-0000-4000-8000-000000000001",
        "owner_id": "learner-00001234",
        "status": "completed",
        "created_at": "2026-11-01T00:00:00+00:00",
        "updated_at": "2026-11-01T00:00:01+00:00",
        "result_json": json.dumps({"stdout": "Hello"}),
    }
    response = request(
        "GET",
        "/v1/jobs/00000000-0000-4000-8000-000000000001",
        headers={"X-Runner-Key": SECRET, "X-CodeLeaf-User": "someone-else"},
    )
    assert response.status_code == 404


def test_get_job_returns_terminal_result_to_owner(monkeypatch):
    setup_api(monkeypatch)
    main.app.state.redis.record = {
        "job_id": "00000000-0000-4000-8000-000000000001",
        "owner_id": "learner-00001234",
        "status": "completed",
        "created_at": "2026-11-01T00:00:00+00:00",
        "updated_at": "2026-11-01T00:00:01+00:00",
        "result_json": json.dumps({"stdout": "Hello CodeLeaf", "outcome": "success"}),
    }
    response = request(
        "GET",
        "/v1/jobs/00000000-0000-4000-8000-000000000001",
        headers={"X-Runner-Key": SECRET, "X-CodeLeaf-User": "learner-00001234"},
    )
    assert response.status_code == 200
    assert response.json()["result"]["stdout"] == "Hello CodeLeaf"


def test_create_job_refuses_language_without_an_installed_piston_runtime(monkeypatch):
    setup_api(monkeypatch)
    response = request(
        "POST",
        "/v1/jobs",
        headers={"X-Runner-Key": SECRET},
        json={
            "owner_id": "learner-00001234",
            "language": "python",
            "source": "print('This must not be faked')",
        },
    )
    assert response.status_code == 409
    assert "has not been installed" in response.json()["detail"]


def test_create_job_rejects_mismatched_owner_scope(monkeypatch):
    setup_api(monkeypatch)
    response = request(
        "POST",
        "/v1/jobs",
        headers={
            "X-Runner-Key": SECRET,
            "X-CodeLeaf-User": "learner-00001234",
        },
        json={
            "owner_id": "another-learner",
            "language": "cpp",
            "source": "int main() { return 0; }",
        },
    )
    assert response.status_code == 400


def test_job_routes_reject_non_uuid_job_identifiers(monkeypatch):
    setup_api(monkeypatch)
    response = request(
        "GET",
        "/v1/jobs/not-a-uuid",
        headers={"X-Runner-Key": SECRET, "X-CodeLeaf-User": "learner-00001234"},
    )
    assert response.status_code == 422


def test_runtime_status_reports_live_runtime_and_package_counts(monkeypatch):
    setup_api(monkeypatch)
    response = request(
        "GET",
        "/v1/runtime-status",
        headers={"X-Runner-Key": SECRET},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["installed_runtime_count"] == 2
    assert data["package_catalogue_version_count"] == 3
    assert data["installed_package_version_count"] == 2
    languages = {item["key"]: item for item in data["languages"]}
    assert languages["cpp"]["available"] is True
    assert languages["javascript"]["runtime_kind"] == "node"
    assert languages["python"]["available"] is False


def test_runtime_status_requires_runner_service_auth(monkeypatch):
    setup_api(monkeypatch)
    response = request("GET", "/v1/runtime-status")
    assert response.status_code == 401
