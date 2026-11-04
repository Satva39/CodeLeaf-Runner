from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from typing import Any
from uuid import UUID

from .config import Settings
from .docker_engine import DockerEngine, DockerEngineError
from .piston import PistonClient, PistonRejected, PistonUnavailable
from .registry import load_registry, runtime_matches

logger = logging.getLogger("codeleaf-runner.database")


class DatabaseExecutionFailure(RuntimeError):
    pass


def _json_value(value: Any, max_cell_bytes: int = 8192) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8", errors="replace")) > max_cell_bytes:
            return value[: max_cell_bytes // 2] + "…[truncated]"
        return value
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        clipped = value[: max_cell_bytes // 2]
        return {"type": "binary", "hex": clipped.hex(), "truncated": len(clipped) < len(value)}
    return str(value)[: max_cell_bytes // 2] + "…[truncated]"


def _make_result(
    *, engine: str, columns: list[str], raw_rows: list[Any], affected_rows: int | None,
    started: float, settings: Settings,
) -> dict[str, Any]:
    safe_columns = [str(column)[:128] for column in columns[:50]]
    rows: list[list[Any]] = []
    used = sum(len(column.encode("utf-8")) for column in safe_columns)
    truncated = len(columns) > 50 or len(raw_rows) > settings.database_max_rows
    for row in raw_rows[: settings.database_max_rows]:
        safe_row: list[Any] = []
        row_bytes = 0
        row_values = list(row)[:50]
        row_overflow = False
        for value in row_values:
            safe_value = _json_value(value)
            encoded = json.dumps(safe_value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            row_bytes += len(encoded)
            safe_row.append(safe_value)
        # Never return partially shaped rows when the response byte budget is reached.
        if used + row_bytes > settings.database_max_result_bytes:
            truncated = True
            row_overflow = True
        if row_overflow:
            break
        used += row_bytes
        rows.append(safe_row)
    return {
        "engine": engine,
        "columns": safe_columns,
        "rows": rows,
        "row_count": len(rows),
        "affected_rows": affected_rows,
        "execution_ms": max(0, int((time.monotonic() - started) * 1000)),
        "truncated": truncated,
        "message": None,
        "error": None,
    }


def _error_result(engine: str, started: float, error: str) -> dict[str, Any]:
    return {
        "engine": engine, "columns": [], "rows": [], "row_count": 0,
        "affected_rows": None, "execution_ms": max(0, int((time.monotonic() - started) * 1000)),
        "truncated": False, "message": None, "error": error[:2000],
    }


def _parse_concatenated_json_arrays(raw: str) -> list[list[dict[str, Any]]]:
    decoder = json.JSONDecoder()
    values: list[list[dict[str, Any]]] = []
    offset = 0
    while offset < len(raw):
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
        if offset >= len(raw):
            break
        value, next_offset = decoder.raw_decode(raw, offset)
        if not isinstance(value, list):
            raise ValueError("SQLite JSON output contained a non-array result.")
        normalized = [item for item in value if isinstance(item, dict)]
        values.append(normalized)
        offset = next_offset
    return values


async def execute_sqlite_via_piston(
    *, setup_statements: list[str], query: str, piston: PistonClient, settings: Settings,
) -> dict[str, Any]:
    """Run SQLite inside Piston/Isolate, never inside the Docker-socket worker."""
    started = time.monotonic()
    try:
        runtimes = await piston.list_runtimes()
        runtime = runtime_matches(load_registry()["sqlite"], runtimes)
        if runtime is None:
            raise DatabaseExecutionFailure("The SQLite runtime is not installed on this Runner node.")
        script_lines = [".bail on", ".headers on", ".mode json"]
        script_lines.extend(f"{statement.rstrip(';')} ;" for statement in setup_statements)
        script_lines.append(f"{query.rstrip(';')} ;")
        query_keyword = re.match(r"\s*([A-Za-z]+)", query)
        # Do not report the setup INSERT count as the affected-row count of a SELECT.
        if query_keyword and query_keyword.group(1).upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            script_lines.append("SELECT changes() AS __codeleaf_affected_rows;")
        response = await piston.execute(
            language="sqlite3",
            version=str(runtime.get("version", "")),
            files=[{"name": "main.sql", "content": "\n".join(script_lines)}],
            stdin="",
            args=[],
        )
        run = response.get("run") if isinstance(response.get("run"), dict) else {}
        stdout = str(run.get("stdout") or "")[: settings.database_max_result_bytes * 2]
        stderr = str(run.get("stderr") or "")[:2048]
        if run.get("code") not in (None, 0) or run.get("status") in {"RE", "SG", "OL", "EL", "XX", "TO"}:
            return {
                "engine": "sqlite", "columns": [], "rows": [], "row_count": 0,
                "affected_rows": None, "execution_ms": int((time.monotonic() - started) * 1000),
                "truncated": False, "message": None,
                "error": (stderr or str(run.get("message") or "SQLite execution failed."))[:2000],
            }
        result_sets = _parse_concatenated_json_arrays(stdout.strip()) if stdout.strip() else []
        affected_rows = None
        if result_sets and result_sets[-1] and "__codeleaf_affected_rows" in result_sets[-1][0]:
            raw_affected = result_sets.pop()[-1].get("__codeleaf_affected_rows")
            affected_rows = int(raw_affected) if raw_affected is not None else None
        records = result_sets[-1] if result_sets else []
        columns = list(records[0].keys()) if records else []
        rows = [tuple(record.get(column) for column in columns) for record in records]
        return _make_result(
            engine="sqlite", columns=columns, raw_rows=rows,
            affected_rows=affected_rows, started=started, settings=settings,
        )
    except (PistonUnavailable, PistonRejected, ValueError, json.JSONDecodeError) as exc:
        return {
            "engine": "sqlite", "columns": [], "rows": [], "row_count": 0,
            "affected_rows": None, "execution_ms": int((time.monotonic() - started) * 1000),
            "truncated": False, "message": None, "error": str(exc)[:2000],
        }


async def _wait_for_postgres(host: str, user: str, password: str, timeout: float) -> None:
    try:
        import asyncpg
    except ImportError as exc:
        raise DatabaseExecutionFailure("PostgreSQL support dependency is not installed.") from exc
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            connection = await asyncpg.connect(
                host=host, port=5432, user=user, password=password,
                database="codeleaf", timeout=2, command_timeout=3,
            )
            await connection.close()
            return
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.35)
    raise DatabaseExecutionFailure("PostgreSQL sandbox did not become ready in time.") from last_error


async def _wait_for_mysql(host: str, user: str, password: str, timeout: float) -> None:
    try:
        import aiomysql
    except ImportError as exc:
        raise DatabaseExecutionFailure("MySQL support dependency is not installed.") from exc
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        connection = None
        try:
            connection = await aiomysql.connect(
                host=host, port=3306, user=user, password=password,
                db="codeleaf", connect_timeout=2, autocommit=True,
            )
            connection.close()
            return
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.35)
        finally:
            if connection is not None:
                connection.close()
    error_code = (
        last_error.args[0]
        if last_error and last_error.args
        and isinstance(last_error.args[0], int)
        else None
    )
    error_type = type(last_error).__name__ if last_error else "UnknownError"
    
    detail = f" ({error_type}, MySQL error code {error_code})" if error_code is not None else f" ({error_type})"
    
    raise DatabaseExecutionFailure(
        f"MySQL sandbox did not become ready in time{detail}."
    ) from last_error


def _postgres_stage_failure(stage: str, exc: Exception) -> DatabaseExecutionFailure:
    """Return a bounded PostgreSQL bootstrap diagnostic without exposing credentials."""
    sqlstate = getattr(exc, "sqlstate", None)
    detail = str(exc).splitlines()[0].strip()[:400] or "No server detail was provided."
    state_text = f"; SQLSTATE {sqlstate}" if isinstance(sqlstate, str) and len(sqlstate) == 5 else ""
    return DatabaseExecutionFailure(
        f"PostgreSQL {stage} failed ({type(exc).__name__}{state_text}): {detail}"
    )


async def _execute_postgres(
    *, host: str, root_user: str, root_password: str, learner_user: str,
    learner_password: str, setup_statements: list[str], query: str, settings: Settings,
    started: float,
) -> dict[str, Any]:
    import asyncpg

    try:
        admin = await asyncpg.connect(
            host=host, port=5432, user=root_user, password=root_password,
            database="codeleaf", timeout=2, command_timeout=4,
        )
    except Exception as exc:
        raise _postgres_stage_failure("administrator connection", exc) from exc
    try:
        try:
            await admin.execute(
                f"CREATE ROLE \"{learner_user}\" WITH LOGIN PASSWORD '{learner_password}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2"
            )
        except Exception as exc:
            raise _postgres_stage_failure("learner-role creation", exc) from exc
        try:
            await admin.execute(f'GRANT CONNECT ON DATABASE codeleaf TO "{learner_user}"')
        except Exception as exc:
            raise _postgres_stage_failure("database CONNECT grant", exc) from exc
        try:
            await admin.execute(
                f'CREATE SCHEMA "{learner_user}" AUTHORIZATION "{learner_user}"'
            )
        except Exception as exc:
            raise _postgres_stage_failure("private schema creation", exc) from exc
    finally:
        await admin.close()

    try:
        connection = await asyncpg.connect(
            host=host, port=5432, user=learner_user, password=learner_password,
            database="codeleaf", timeout=3, command_timeout=settings.database_query_timeout_seconds + 2,
            server_settings={
                "statement_timeout": str(int(settings.database_query_timeout_seconds * 1000)),
                "lock_timeout": "750",
                "idle_in_transaction_session_timeout": "3000",
                # temp_file_limit is a superuser-context setting in PostgreSQL.
                # It is enforced globally by the disposable server at startup; do not
                # ask the restricted learner role to set it during connection startup.
                "work_mem": "4096",
                "search_path": f'"{learner_user}"',
            },
        )
    except Exception as exc:
        raise _postgres_stage_failure("learner connection", exc) from exc
    try:
        try:
            for statement in setup_statements:
                await connection.execute(statement, timeout=settings.database_query_timeout_seconds)
            prepared = await connection.prepare(query, timeout=settings.database_query_timeout_seconds)
            attributes = prepared.get_attributes()
            if attributes:
                columns = [attribute.name for attribute in attributes]
                # Use a server-side cursor and fetch only one row beyond the UI cap.
                # This prevents a learner's large SELECT from filling worker memory.
                raw_rows = []
                async with connection.transaction():
                    # Manual fetch mode: asyncpg only permits ``prefetch`` when the
                    # CursorFactory is consumed with ``async for``.  This code awaits
                    # the factory and calls Cursor.fetch(), so prefetch must be omitted.
                    cursor_factory = prepared.cursor(
                        timeout=settings.database_query_timeout_seconds
                    )
                    cursor = await cursor_factory
                    # Fetch one row at a time; stop as soon as the response cap is reached.
                    for _ in range(settings.database_max_rows + 1):
                        records = await cursor.fetch(1, timeout=settings.database_query_timeout_seconds)
                        if not records:
                            break
                        record = records[0]
                        raw_rows.append(tuple(record[index] for index in range(len(columns))))
                        probe = _make_result(
                            engine="postgresql", columns=columns, raw_rows=raw_rows,
                            affected_rows=None, started=started, settings=settings,
                        )
                        if probe["truncated"] or probe["row_count"] < len(raw_rows):
                            break
                affected_rows = None
            else:
                status = await prepared.execute(timeout=settings.database_query_timeout_seconds)
                columns = []
                raw_rows = []
                affected_rows = _affected_count(status)
            return _make_result(
                engine="postgresql", columns=columns, raw_rows=raw_rows,
                affected_rows=affected_rows, started=started, settings=settings,
            )
        except asyncpg.PostgresError as exc:
            return _error_result("postgresql", started, f"{type(exc).__name__}: {exc}")
    finally:
        await connection.close()


async def _execute_mysql(
    *, host: str, learner_user: str, learner_password: str,
    setup_statements: list[str], query: str, settings: Settings, started: float,
) -> dict[str, Any]:
    import aiomysql
    from aiomysql.cursors import SSCursor

    connection = await aiomysql.connect(
        host=host, port=3306, user=learner_user, password=learner_password,
        db="codeleaf", connect_timeout=3, autocommit=True, cursorclass=SSCursor,
    )
    try:
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("SET SESSION max_execution_time=3000")
                await cursor.execute("SET SESSION innodb_lock_wait_timeout=2")
                for statement in setup_statements:
                    await cursor.execute(statement)
                await cursor.execute(query)
                columns = [item[0] for item in cursor.description] if cursor.description else []
                if columns:
                    raw_rows = []
                    for _ in range(settings.database_max_rows + 1):
                        batch = await cursor.fetchmany(1)
                        if not batch:
                            break
                        raw_rows.append(tuple(batch[0]))
                        probe = _make_result(
                            engine="mysql", columns=columns, raw_rows=raw_rows,
                            affected_rows=None, started=started, settings=settings,
                        )
                        if probe["truncated"] or probe["row_count"] < len(raw_rows):
                            break
                    affected_rows = None
                else:
                    raw_rows = []
                    affected_rows = max(cursor.rowcount, 0)
            return _make_result(
                engine="mysql", columns=columns, raw_rows=raw_rows,
                affected_rows=affected_rows, started=started, settings=settings,
            )
        except Exception as exc:
            # aiomysql exposes PyMySQL's DB-API errors; report safe SQL errors to the learner.
            if type(exc).__module__.startswith(("pymysql", "aiomysql")):
                return _error_result("mysql", started, f"{type(exc).__name__}: {exc}")
            raise
    finally:
        connection.close()


def _affected_count(status: str) -> int | None:
    match = re.search(r"(?:INSERT\s+\d+\s+|UPDATE\s+|DELETE\s+|MERGE\s+)(\d+)", status.upper())
    if match:
        return int(match.group(1))
    if status.upper().startswith(("CREATE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE")):
        return 0
    return None


async def execute_database_job(
    *, engine: str, setup_statements: list[str], query: str, job_id: str,
    settings: Settings, docker: DockerEngine | None, piston: PistonClient,
) -> dict[str, Any]:
    started = time.monotonic()
    if engine == "sqlite":
        return await execute_sqlite_via_piston(
            setup_statements=setup_statements, query=query, piston=piston, settings=settings,
        )
    if engine not in {"mysql", "postgresql"}:
        raise DatabaseExecutionFailure("Unsupported database engine.")
    if docker is None:
        raise DatabaseExecutionFailure("Disposable database execution is not configured on this Runner node.")

    image = settings.database_mysql_image if engine == "mysql" else settings.database_postgres_image
    root_password = secrets.token_urlsafe(32)
    learner_password = secrets.token_urlsafe(32)
    suffix = job_id.replace("-", "")[:20]
    learner_user = f"learner_{suffix}"
    root_user = f"admin_{suffix}"
    container_id: str | None = None
    network_id: str | None = None
    try:
        container_id, host, network_id = await docker.create_ephemeral_database(
            engine=engine, job_id=job_id, image=image,
            worker_container_name=settings.database_worker_container_name,
            root_password=root_password,
            database_user=learner_user,
            database_password=learner_password,
        )
        if engine == "postgresql":
            await _wait_for_postgres(host, root_user, root_password, settings.database_startup_timeout_seconds)
            return await _execute_postgres(
                host=host, root_user=root_user, root_password=root_password,
                learner_user=learner_user, learner_password=learner_password,
                setup_statements=setup_statements, query=query,
                settings=settings, started=started,
            )
        await _wait_for_mysql(host, learner_user, learner_password, settings.database_startup_timeout_seconds)
        return await _execute_mysql(
            host=host, learner_user=learner_user, learner_password=learner_password,
            setup_statements=setup_statements, query=query,
            settings=settings, started=started,
        )
    except asyncio.TimeoutError:
        return {
            "engine": engine, "columns": [], "rows": [], "row_count": 0,
            "affected_rows": None, "execution_ms": int((time.monotonic() - started) * 1000),
            "truncated": False, "message": None,
            "error": "Database execution exceeded the configured time limit.",
        }
    except (DatabaseExecutionFailure, DockerEngineError) as exc:
        return {
            "engine": engine, "columns": [], "rows": [], "row_count": 0,
            "affected_rows": None, "execution_ms": int((time.monotonic() - started) * 1000),
            "truncated": False, "message": None, "error": str(exc)[:2000],
        }
    except Exception as exc:
        logger.warning("Database execution failed: engine=%s job=%s error=%s detail=%s", engine, job_id, type(exc).__name__, str(exc)[:500])
        return {
            "engine": engine, "columns": [], "rows": [], "row_count": 0,
            "affected_rows": None, "execution_ms": int((time.monotonic() - started) * 1000),
            "truncated": False, "message": None,
            "error": "The database sandbox could not complete this execution.",
        }
    finally:
        if container_id or network_id:
            try:
                await docker.remove_ephemeral_database(
                    container_id, network_id, settings.database_worker_container_name
                )
            except DockerEngineError:
                # Leave labels/resources for the database worker's orphan cleanup loop.
                logger.exception("Could not remove temporary database resources for job %s", job_id)
