from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import auth, main
from app.config import Settings
from app.database_execution import (
    _postgres_stage_failure, execute_database_job, execute_sqlite_via_piston,
)
from app.database_jobs import DATABASE_WORKER_HEARTBEAT_KEY
from app.docker_engine import DockerEngine
from app.sql_validation import split_sql, validate_database_sql

SECRET = "phase-four-database-test-secret-with-at-least-32-characters"


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.actions = []

    def hset(self, key, mapping):
        self.actions.append(("hset", key, mapping))
        return self

    def srem(self, key, value):
        self.actions.append(("srem", key, value))
        return self

    def expire(self, key, ttl):
        self.actions.append(("expire", key, ttl))
        return self

    def delete(self, key):
        self.actions.append(("delete", key))
        return self

    def xack(self, *args):
        self.actions.append(("xack", args))
        return self

    def xdel(self, *args):
        self.actions.append(("xdel", args))
        return self

    async def execute(self):
        for action in self.actions:
            if action[0] == "hset":
                self.redis.records.setdefault(action[1], {}).update(action[2])
        return []


class FakeRedis:
    def __init__(self):
        self.records = {}
        self.values = {DATABASE_WORKER_HEARTBEAT_KEY: "alive"}
        self.last_eval = None

    async def get(self, key):
        return self.values.get(key)

    async def eval(self, script, count, *args):
        self.last_eval = (script, count, args)
        from app.security import RATE_LIMIT_LUA
        if script == RATE_LIMIT_LUA:
            return [1, 60, 1]
        if "CREATE_DATABASE_JOB" in script or "DATABASE_STREAM" in script or "local active_key" in script:
            job_id, owner_id, engine, payload_json, *_ = args[4:]
            self.records[f"codeleaf:runner:database-job:{job_id}"] = {
                "job_id": job_id,
                "owner_id": owner_id,
                "engine": engine,
                "status": "queued",
                "payload_json": payload_json,
                "result_json": "{}",
                "created_at": "2026-11-02T00:00:00+00:00",
                "updated_at": "2026-11-02T00:00:00+00:00",
            }
            return ["ok", "1-0"]
        return ["missing"]

    async def hgetall(self, key):
        return self.records.get(key, {})

    def pipeline(self, transaction=True):
        return FakePipeline(self)


class FakeDockerResponse:
    status_code = 404
    def json(self):
        return {}


class FakeDockerHttp:
    async def get(self, *_args, **_kwargs):
        return FakeDockerResponse()


class FakePiston:
    def __init__(self):
        self.payload = None

    async def list_runtimes(self):
        return [{"language": "sqlite3", "version": "3.36.0", "aliases": ["sqlite"]}]

    async def execute(self, **kwargs):
        self.payload = kwargs
        stdout = '[{"id":1,"name":"Ada"}]\n'
        if "__codeleaf_affected_rows" in self.payload["files"][0]["content"]:
            stdout += '[{"__codeleaf_affected_rows":1}]\n'
        return {
            "run": {"stdout": stdout, "stderr": "", "code": 0, "status": "success"}
        }


def api_request(method, url, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, url, **kwargs)
    return asyncio.run(run())


def setup_api(monkeypatch):
    settings = Settings(service_token=SECRET, database_execution_enabled=True)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    main.app.state.redis = FakeRedis()
    main.app.state.piston = FakePiston()
    return main.app.state.redis



def test_postgres_bootstrap_diagnostic_includes_stage_and_sqlstate():
    class PermissionErrorForTest(Exception):
        sqlstate = "42501"

    error = _postgres_stage_failure(
        "learner-role creation",
        PermissionErrorForTest("permission denied to create role"),
    )
    assert "learner-role creation" in str(error)
    assert "InsufficientPrivilegeError" not in str(error)
    assert "SQLSTATE 42501" in str(error)
    assert "permission denied to create role" in str(error)


def test_sql_splitter_respects_semicolons_inside_literals_and_comments():
    source = "CREATE TABLE t (v TEXT); INSERT INTO t VALUES ('a;b'); -- x;y\nUPDATE t SET v='z';"
    assert len(split_sql(source)) == 3


def test_sql_splitter_respects_postgres_dollar_quoted_function_body():
    source = "CREATE FUNCTION f() RETURNS void AS $$ BEGIN RAISE NOTICE 'a;b'; END; $$ LANGUAGE plpgsql; SELECT 1;"
    assert len(split_sql(source)) == 2


def test_sql_validation_allows_setup_batch_and_one_query():
    setup, query = validate_database_sql(
        "CREATE TABLE people(id INTEGER); INSERT INTO people VALUES(1);",
        "SELECT * FROM people;",
    )
    assert len(setup) == 2
    assert query.startswith("SELECT")


def test_sql_validation_rejects_multiple_query_statements():
    with pytest.raises(Exception):
        validate_database_sql("", "SELECT 1; SELECT 2;")


def test_sql_validation_rejects_shell_meta_commands():
    with pytest.raises(Exception):
        validate_database_sql("", ".shell whoami\nSELECT 1;")


def test_sqlite_runs_inside_piston_and_parses_structured_result():
    settings = Settings(service_token=SECRET)
    piston = FakePiston()
    result = asyncio.run(execute_sqlite_via_piston(
        setup_statements=["CREATE TABLE people(id INTEGER, name TEXT)", "INSERT INTO people VALUES (1, 'Ada')"],
        query="SELECT id, name FROM people",
        piston=piston,
        settings=settings,
    ))
    assert result["columns"] == ["id", "name"]
    assert result["rows"] == [[1, "Ada"]]
    assert result["affected_rows"] is None
    assert piston.payload["language"] == "sqlite3"
    script = piston.payload["files"][0]["content"]
    assert ".mode json" in script
    assert ".bail on" in script
    assert "CREATE TABLE" in script


def test_sqlite_reports_piston_execution_errors():
    class BrokenPiston(FakePiston):
        async def execute(self, **kwargs):
            return {"run": {"stdout": "", "stderr": "near SELECT: syntax error", "code": 1, "status": "RE"}}
    result = asyncio.run(execute_sqlite_via_piston(
        setup_statements=[], query="SELECT * FROM missing_table",
        piston=BrokenPiston(), settings=Settings(service_token=SECRET),
    ))
    assert "syntax error" in result["error"]


def test_database_job_sqlite_does_not_need_docker_socket():
    settings = Settings(service_token=SECRET)
    result = asyncio.run(execute_database_job(
        engine="sqlite", setup_statements=[], query="SELECT 1 AS value",
        job_id="00000000-0000-4000-8000-000000000001", settings=settings,
        docker=None, piston=FakePiston(),
    ))
    assert result["rows"] == [[1, "Ada"]]


def test_docker_database_config_is_isolated_and_has_no_host_ports():
    class FakeDocker(DockerEngine):
        def __init__(self):
            self.config = None
            self.image = None
            self.prefix = "/v1.45"
            self.client = FakeDockerHttp()
        async def ensure_image(self, image):
            self.image = image
        async def create_internal_network(self, job_id):
            self.job_id = job_id
            return "network-id-123456", "codeleaf-db-net-test"
        async def connect_worker_to_network(self, network_id, worker_container_name):
            self.worker_attached = (network_id, worker_container_name)
        async def create_container(self, *, name, config):
            self.config = config
            return "container-id-123456"
        async def start_container(self, container_id):
            return None
        async def remove_container(self, container_id):
            return None
        async def disconnect_worker_from_network(self, network_id, worker_container_name):
            self.worker_detached = (network_id, worker_container_name)
        async def remove_internal_network(self, network_id):
            self.removed_network = network_id
    docker = FakeDocker()
    container_id, name, network_id = asyncio.run(docker.create_ephemeral_database(
        engine="postgresql", job_id="00000000-0000-4000-8000-000000000001",
        image="postgres:17.11-bookworm", worker_container_name="codeleaf-runner-database-worker",
        root_password="root-secret", database_user="learner_123", database_password="user-secret",
    ))
    host = docker.config["HostConfig"]
    assert container_id == "container-id-123456"
    assert name.startswith("codeleaf-db-postgresql-")
    assert network_id == "network-id-123456"
    assert host["NetworkMode"] == "codeleaf-db-net-test"
    assert docker.config["Labels"]["codeleaf.runner.database-network-id"] == network_id
    assert docker.worker_attached == (network_id, "codeleaf-runner-database-worker")
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert host["PidsLimit"] == 64
    assert host["Memory"] == 536870912
    assert host["Tmpfs"]["/var/lib/postgresql/data"].find("size=256m") >= 0
    # Enforce the temporary-file budget at server startup, where it is applied
    # with administrator privileges rather than requested by the learner role.
    assert "temp_file_limit=32MB" in docker.config["Cmd"]
    assert "PortBindings" not in host
    assert "Binds" not in host


def test_mysql_container_has_no_public_port_or_persistent_volume():
    class FakeDocker(DockerEngine):
        def __init__(self):
            self.config = None
            self.prefix = "/v1.45"
            self.client = FakeDockerHttp()
        async def ensure_image(self, image):
            pass
        async def create_internal_network(self, job_id):
            return "mysql-network-id-123", "codeleaf-db-net-mysql"
        async def connect_worker_to_network(self, network_id, worker_container_name):
            self.worker_attached = True
        async def create_container(self, *, name, config):
            self.config = config
            return "container-id-123456"
        async def start_container(self, container_id):
            return None
        async def remove_container(self, container_id):
            return None
        async def disconnect_worker_from_network(self, network_id, worker_container_name):
            self.worker_detached = True
        async def remove_internal_network(self, network_id):
            self.network_removed = True
    docker = FakeDocker()
    _, _, network_id = asyncio.run(docker.create_ephemeral_database(
        engine="mysql", job_id="00000000-0000-4000-8000-000000000002",
        image="mysql:8.4.13", worker_container_name="codeleaf-runner-database-worker",
        root_password="root-secret", database_user="learner_123", database_password="user-secret",
    ))
    host = docker.config["HostConfig"]
    assert network_id == "mysql-network-id-123"
    assert host["NetworkMode"] == "codeleaf-db-net-mysql"
    assert host["ReadonlyRootfs"] is True
    assert "PortBindings" not in host
    assert "Binds" not in host


def test_database_engine_endpoint_reports_worker_and_sqlite_runtime(monkeypatch):
    setup_api(monkeypatch)
    response = api_request("GET", "/v1/database/engines", headers={"X-Runner-Key": SECRET})
    assert response.status_code == 200
    engines = {item["key"]: item for item in response.json()["engines"]}
    assert engines["sqlite"]["available"] is True
    assert engines["mysql"]["available"] is True
    assert engines["postgresql"]["available"] is True


def test_database_job_api_enqueues_owner_scoped_job(monkeypatch):
    setup_api(monkeypatch)
    response = api_request(
        "POST", "/v1/database/jobs",
        headers={"X-Runner-Key": SECRET, "X-CodeLeaf-User": "learner-123"},
        json={
            "engine": "sqlite",
            "setup_sql": "CREATE TABLE people(id INTEGER); INSERT INTO people VALUES(1);",
            "query": "SELECT * FROM people;",
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_database_job_api_rejects_missing_worker_heartbeat(monkeypatch):
    redis = setup_api(monkeypatch)
    redis.values.pop(DATABASE_WORKER_HEARTBEAT_KEY, None)
    response = api_request(
        "POST", "/v1/database/jobs",
        headers={"X-Runner-Key": SECRET, "X-CodeLeaf-User": "learner-123"},
        json={"engine": "sqlite", "query": "SELECT 1"},
    )
    assert response.status_code == 503


def test_database_job_api_rejects_multi_statement_query(monkeypatch):
    setup_api(monkeypatch)
    response = api_request(
        "POST", "/v1/database/jobs",
        headers={"X-Runner-Key": SECRET, "X-CodeLeaf-User": "learner-123"},
        json={"engine": "mysql", "query": "SELECT 1; SELECT 2;"},
    )
    assert response.status_code == 422


def test_database_job_api_is_owner_scoped(monkeypatch):
    redis = setup_api(monkeypatch)
    job_id = "00000000-0000-4000-8000-000000000123"
    redis.records[f"codeleaf:runner:database-job:{job_id}"] = {
        "job_id": job_id,
        "owner_id": "learner-1",
        "engine": "postgresql",
        "status": "completed",
        "result_json": json.dumps({"rows": [[1]]}),
        "created_at": "2026-11-02T00:00:00+00:00",
        "updated_at": "2026-11-02T00:00:01+00:00",
    }
    response = api_request(
        "GET", f"/v1/database/jobs/{job_id}",
        headers={"X-Runner-Key": SECRET, "X-CodeLeaf-User": "learner-2"},
    )
    assert response.status_code == 404


def test_result_byte_limit_never_returns_partial_rows():
    settings = Settings(service_token=SECRET, database_max_result_bytes=1024)
    from app.database_execution import _make_result
    result = _make_result(
        engine="sqlite", columns=["id", "value"], raw_rows=[(1, "x" * 2000)],
        affected_rows=None, started=__import__("time").monotonic(), settings=settings,
    )
    assert result["rows"] == []
    assert result["row_count"] == 0
    assert result["truncated"] is True


def test_ephemeral_database_config_labels_each_job_network():
    class FakeDocker(DockerEngine):
        def __init__(self):
            self.config = None
            self.client = FakeDockerHttp()
            self.prefix = "/v1.45"
        async def ensure_image(self, image):
            pass
        async def create_internal_network(self, job_id):
            return "network-id-987654", "isolated-job-network"
        async def connect_worker_to_network(self, network_id, worker_container_name):
            pass
        async def create_container(self, *, name, config):
            self.config = config
            return "container-id-987654"
        async def start_container(self, container_id):
            pass
    docker = FakeDocker()
    _, _, network_id = asyncio.run(docker.create_ephemeral_database(
        engine="postgresql", job_id="00000000-0000-4000-8000-000000000003",
        image="postgres:17.11-bookworm", worker_container_name="codeleaf-runner-database-worker",
        root_password="secret", database_user="learner_test", database_password="secret",
    ))
    assert network_id == "network-id-987654"
    assert docker.config["HostConfig"]["NetworkMode"] == "isolated-job-network"
    assert docker.config["Labels"]["codeleaf.runner.database-job"] == "00000000-0000-4000-8000-000000000003"
    assert docker.config["Labels"]["codeleaf.runner.database-network-id"] == network_id


def test_docker_internal_network_is_created_with_internal_flag():
    class Response:
        status_code = 201
        def json(self):
            return {"Id": "network-created-123456"}
    class Client:
        def __init__(self):
            self.payload = None
        async def post(self, path, json):
            self.payload = (path, json)
            return Response()
    docker = DockerEngine.__new__(DockerEngine)
    docker.client = Client()
    docker.prefix = "/v1.45"
    network_id, name = asyncio.run(docker.create_internal_network("00000000-0000-4000-8000-000000000111"))
    assert network_id == "network-created-123456"
    assert name.startswith("codeleaf-db-net-")
    assert docker.client.payload[1]["Internal"] is True
    assert docker.client.payload[1]["Labels"]["codeleaf.runner.managed"] == "true"


def test_ephemeral_cleanup_removes_container_then_detaches_and_removes_network():
    class FakeDocker(DockerEngine):
        def __init__(self):
            self.events = []
        async def remove_container(self, container_id):
            self.events.append(("container", container_id))
        async def disconnect_worker_from_network(self, network_id, worker_container_name):
            self.events.append(("disconnect", network_id, worker_container_name))
        async def remove_internal_network(self, network_id):
            self.events.append(("network", network_id))
    docker = FakeDocker()
    asyncio.run(docker.remove_ephemeral_database(
        "container-123456", "network-123456", "codeleaf-runner-database-worker"
    ))
    assert docker.events == [
        ("container", "container-123456"),
        ("disconnect", "network-123456", "codeleaf-runner-database-worker"),
        ("network", "network-123456"),
    ]


def test_mysql_execution_always_cleans_its_container_and_network(monkeypatch):
    from app import database_execution
    cleanup_calls = []
    class FakeDocker:
        async def create_ephemeral_database(self, **kwargs):
            assert kwargs["worker_container_name"] == "codeleaf-runner-database-worker"
            return "container-123456", "codeleaf-db-mysql-job", "network-123456"
        async def remove_ephemeral_database(self, container_id, network_id, worker_name):
            cleanup_calls.append((container_id, network_id, worker_name))
    async def fake_wait(*args, **kwargs):
        return None
    async def fake_execute(**kwargs):
        return {"engine": "mysql", "columns": ["value"], "rows": [[1]], "row_count": 1, "error": None}
    monkeypatch.setattr(database_execution, "_wait_for_mysql", fake_wait)
    monkeypatch.setattr(database_execution, "_execute_mysql", fake_execute)
    result = asyncio.run(database_execution.execute_database_job(
        engine="mysql", setup_statements=[], query="SELECT 1", job_id="00000000-0000-4000-8000-000000000222",
        settings=Settings(service_token=SECRET), docker=FakeDocker(), piston=FakePiston(),
    ))
    assert result["rows"] == [[1]]
    assert cleanup_calls == [("container-123456", "network-123456", "codeleaf-runner-database-worker")]


def test_mysql_execution_cleans_resources_after_query_exception(monkeypatch):
    from app import database_execution
    cleanup_calls = []
    class FakeDocker:
        async def create_ephemeral_database(self, **kwargs):
            return "container-123456", "codeleaf-db-mysql-job", "network-123456"
        async def remove_ephemeral_database(self, container_id, network_id, worker_name):
            cleanup_calls.append((container_id, network_id))
    async def fake_wait(*args, **kwargs):
        return None
    async def fake_execute(**kwargs):
        raise RuntimeError("simulated database error")
    monkeypatch.setattr(database_execution, "_wait_for_mysql", fake_wait)
    monkeypatch.setattr(database_execution, "_execute_mysql", fake_execute)
    result = asyncio.run(database_execution.execute_database_job(
        engine="mysql", setup_statements=[], query="SELECT 1", job_id="00000000-0000-4000-8000-000000000223",
        settings=Settings(service_token=SECRET), docker=FakeDocker(), piston=FakePiston(),
    ))
    assert result["error"] == "The database sandbox could not complete this execution."
    assert cleanup_calls == [("container-123456", "network-123456")]


def test_sqlite_select_does_not_report_setup_insert_as_query_affected_rows():
    piston = FakePiston()
    asyncio.run(execute_sqlite_via_piston(
        setup_statements=["INSERT INTO people VALUES (1, 'Ada')"],
        query="SELECT * FROM people", piston=piston,
        settings=Settings(service_token=SECRET),
    ))
    script = piston.payload["files"][0]["content"]
    assert "SELECT changes() AS __codeleaf_affected_rows" not in script


def test_postgres_uses_awaited_server_cursor_and_bounded_rows(monkeypatch):
    import sys
    import types
    from app.database_execution import _execute_postgres

    class Attribute:
        def __init__(self, name):
            self.name = name

    class FakeCursor:
        def __init__(self):
            self.calls = 0
        async def fetch(self, count, timeout=None):
            self.calls += 1
            return [(1, "Ada")] if self.calls == 1 else []

    class CursorFactory:
        def __await__(self):
            async def create():
                return FakeCursor()
            return create().__await__()

    class Prepared:
        def get_attributes(self):
            return [Attribute("id"), Attribute("name")]
        def cursor(self, *args, prefetch=None, timeout=None):
            assert args == ()
            # Manual fetch mode must not set prefetch; asyncpg rejects it when
            # the CursorFactory is awaited rather than asynchronously iterated.
            assert prefetch is None
            assert timeout is not None and timeout > 0
            return CursorFactory()

    class Transaction:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False

    admin_statements = []
    learner_connect_kwargs = {}

    class FakeAdmin:
        async def execute(self, sql):
            admin_statements.append(sql)
            return "OK"
        async def close(self):
            return None

    class FakeLearner:
        def transaction(self):
            return Transaction()
        async def prepare(self, _query, timeout=None):
            return Prepared()
        async def close(self):
            return None

    async def connect(**kwargs):
        if kwargs["user"].startswith("admin_"):
            return FakeAdmin()
        learner_connect_kwargs.update(kwargs)
        return FakeLearner()

    fake_module = types.ModuleType("asyncpg")
    fake_module.connect = connect
    fake_module.PostgresError = type("PostgresError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "asyncpg", fake_module)

    result = asyncio.run(_execute_postgres(
        host="db", root_user="admin_test", root_password="root-secret",
        learner_user="learner_test", learner_password="user-secret",
        setup_statements=[], query="SELECT 1 AS id, 'Ada' AS name",
        settings=Settings(service_token=SECRET), started=__import__("time").monotonic(),
    ))
    assert result["columns"] == ["id", "name"]
    assert result["rows"] == [[1, "Ada"]]
    assert result["row_count"] == 1
    assert any('CREATE SCHEMA "learner_test" AUTHORIZATION "learner_test"' in sql for sql in admin_statements)
    assert not any("ALTER SCHEMA public OWNER" in sql for sql in admin_statements)
    assert learner_connect_kwargs["server_settings"]["search_path"] == '"learner_test"'
    # temp_file_limit is already applied by the disposable PostgreSQL server as an
    # administrator-level startup setting; the restricted learner must not set it.
    assert "temp_file_limit" not in learner_connect_kwargs["server_settings"]


def test_database_smoke_test_uses_a_unique_owner_per_invocation():
    from scripts import smoke_test_database_engines
    import re
    assert re.fullmatch(r"codeleaf-db-smoke-[0-9a-f]{32}", smoke_test_database_engines.OWNER_ID)


def test_database_worker_compose_does_not_inherit_full_env_or_api_token():
    from pathlib import Path
    import yaml

    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["database-worker"]
    assert "env_file" not in service
    environment = service["environment"]
    assert "CODELEAF_RUNNER_SERVICE_TOKEN" not in environment
    assert "REDIS_URL" in environment
    assert "PISTON_BASE_URL" in environment
    assert "RUNNER_DATABASE_DOCKER_SOCKET" in environment
    assert not any("docker.sock" in str(volume) for volume in service.get("volumes", []))
    assert service["environment"]["RUNNER_DATABASE_DOCKER_SOCKET"] == "http://docker-gateway:2375"
    assert "docker-control" in service["networks"]
