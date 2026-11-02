# Phase 4 — Database execution

## Scope delivered

- [x] A separate Redis Stream queue and worker for SQL execution.
- [x] The Docker-socket database worker receives only explicitly allow-listed settings; it does not inherit `.env` or the Runner API service token.
- [x] SQLite in-memory execution through Piston/Isolate. SQLite never runs in the process that has Docker socket access.
- [x] MySQL 8.4 and PostgreSQL 17 use fixed, disposable per-job containers.
- [x] A fresh Docker `internal` bridge network is created for every MySQL/PostgreSQL job.
- [x] Only the database-worker joins a job network; the matching database container is the only other member.
- [x] Database containers have no host port, no persistent volume, read-only root filesystem, memory/CPU/PID/file limits, reduced Linux capabilities, and no-new-privileges.
- [x] MySQL uses the scoped learner account created by the official image. PostgreSQL creates a non-superuser learner role and a dedicated per-job schema owned by that learner; the built-in `public` schema is not modified.
- [x] Query/setup validation, statement count, payload size, result row/column/byte limits, per-user quotas, queue depth and job timeout.
- [x] Owner-scoped job create, poll and queued-job cancellation API.
- [x] Database worker heartbeat and stale-container/network cleanup.
- [x] End-to-end smoke-test script for SQLite, MySQL and PostgreSQL.
- [x] Setuptools package discovery is explicit so `pip install -e ".[test]"` does not auto-discover `config/` as a Python package.

## API

- `GET /v1/database/engines`
- `POST /v1/database/jobs`
- `GET /v1/database/jobs/{job_id}`
- `DELETE /v1/database/jobs/{job_id}` (queued jobs only; returns 409 after a job starts)

The API requires `X-Runner-Key` and `X-CodeLeaf-User`. The owner ID must come from the authenticated CodeLeaf backend, never from an untrusted browser field.

## Windows development verification

Install dependencies and test tools from the project root:

```powershell
python -m pip install -r requirements.txt
python -m pip install -e ".[test]"
python -m pytest -q
```

If build isolation cannot download setuptools/wheel in a restricted environment, install the build tools first where network access is available, then use:

```powershell
python -m pip install --upgrade pip setuptools wheel
python -m pip install --no-build-isolation -e ".[test]"
```

Start/rebuild the services:

```powershell
docker compose config --quiet
docker compose up -d --build --force-recreate api worker database-worker piston
```

Check the database worker and run all three live SQL smoke tests:

```powershell
docker compose ps -a
docker compose logs --tail=80 database-worker
python scripts/smoke_test_database_engines.py
```

The smoke script reads the existing local `.env` service token through the app settings. It does not print the token. It uses a fresh, generated test owner ID per invocation so repeated diagnostic runs do not exhaust one fixed test identity's per-minute quota. It submits a disposable create/insert/select program to each engine and checks for `[[1, "CodeLeaf"]]`.

## PostgreSQL learner isolation

Each PostgreSQL job creates a dedicated schema named after its random learner role and sets that role's `search_path` to only that schema. The job does not change ownership or privileges on PostgreSQL's built-in `public` schema. The learner role is not a superuser and cannot create roles or databases.
PostgreSQL `temp_file_limit` is enforced by the disposable server at startup (`-c temp_file_limit=32MB`). It is intentionally omitted from learner connection settings because PostgreSQL restricts that parameter to privileged roles.

## Important operational constraints

- Run this Docker-socket design only on a dedicated execution host with no production database or app secrets. Access to the Docker Engine socket is effectively host-level administrative access.
- Do not expose the Docker socket, Redis, Piston port 2000, or the Runner API publicly.
- Per-job networks are marked `Internal=true`, have no published ports, and are removed with each database container.
- Database state is ephemeral. Every job starts a fresh database and all tables/data are destroyed after its result is captured. It is not a persistent SQL workspace.
- SQLite runs through Piston's Isolate path, not in the Docker-socket database worker.
- MySQL/PostgreSQL are not production DB proxies. No Neon, Catalyst, or CodeLeaf production credentials are configured or passed to these containers.
- This phase has unit/API tests, but a live database smoke test must pass on the target Docker host before declaring Phase 4 operationally verified.


## PostgreSQL troubleshooting after the learner-schema correction

If PostgreSQL still fails during bootstrap, the API result now identifies the failing stage (administrator connection, learner-role creation, database CONNECT grant, private schema creation, or learner connection), includes the PostgreSQL SQLSTATE when available, and returns a bounded first-line server diagnostic. This is intentionally limited to the disposable per-job database. Do not add database passwords or service tokens to logs.

Rebuild the database worker after applying the diagnostic patch:

```powershell
docker compose up -d --build --force-recreate database-worker
python scripts/smoke_test_database_engines.py
```

If PostgreSQL fails, inspect the latest job's returned `result.error` or run `docker compose logs --since=5m database-worker`. The stage and SQLSTATE identify the missing PostgreSQL privilege without logging credentials.
