# CodeLeaf Runner — Phases 1–5

A standalone execution service for CodeLeaf. It provides a private FastAPI control plane, a durable Redis Streams queue, worker scheduling, a live Piston runtime catalogue, runtime provisioning, multi-file and interactive execution, a separate database-execution service for SQLite, MySQL and PostgreSQL, and production security/operations hardening.

**This repository is separate from the existing CodeLeaf website/backend repository.** It does not replace or edit `Code_Leaf/`.

## Phase 1 deliverable — service foundation

- Private service-to-service authentication using `X-Runner-Key`.
- Per-job owner scope using `X-CodeLeaf-User` (plus the existing CodeLeaf POST-body compatibility field `owner_id`).
- Language registry containing 81 Piston language identifiers/aliases.
- Redis Streams queue with consumer groups, worker concurrency, pending-message recovery, queue-depth limit, per-user active-job limit, and per-user rate limit.
- Piston REST adapter with explicit compile/run CPU, wall-time, memory, file-count, input, and output bounds.
- Multi-file source support and pre-supplied standard input.
- Normalized `stdout`, `stderr`, compiler output, exit codes, runtime version, execution timing and memory fields.
- API routes compatible with the current CodeLeaf `/v1/jobs` create/poll/cancel contract.
- Docker Compose for API, worker, Redis, and private Piston engine.

## Phase 2 deliverable — language runtime provisioning

- Live Piston package-catalogue discovery (`GET /api/v2/packages`).
- Safe-by-default provisioning tool with `core`, `extended`, and dynamically discovered `all` profiles.
- Exact-version selection, optional runtime lock files, and package-install confirmation.
- Runtime verification against the actual `GET /api/v2/runtimes` endpoint.
- Real C/C++, Python, JavaScript, Java, C#, Go, Rust, PHP, Ruby, Kotlin, Swift, Dart, R, shell, Scala, and SQLite smoke-test programs.
- Authenticated `/v1/runtime-status` endpoint and enriched `/v1/languages` availability information.
- SQL runtime at this phase means SQLite. MySQL/PostgreSQL and scientific Python libraries need separate runtime strategies.

See [`docs/RUNTIME_INSTALLATION.md`](docs/RUNTIME_INSTALLATION.md) for the administrator workflow. The provisioning scripts do not install anything unless `--apply` is supplied.

## Phase 3 deliverable — multi-file and interactive terminal execution

- Multi-file compilation/interpreted execution with up to 25 validated flat root-level files; first file is the Piston main file.
- Larger bounded workspaces (256 KiB per file, 1 MiB total source).
- Private `/v1/sessions` WebSocket proxy over Piston's self-hosted `/api/v2/connect` protocol.
- Live stdin, stdout and stderr streaming, compile/run stage events, exit events and process signals.
- Owner-scoped session cancellation through `DELETE /v1/sessions/{session_id}` and Redis Pub/Sub control.
- Per-user session quotas, idle and total-session timeouts, message count and byte limits.
- C/C++ runtime installation mapping through GCC and C# mapping through dotnet when advertised by the live Piston package catalogue.
- More realistic compile allowance and file-descriptor limit for Kotlin, Scala and R runtime starts.

See [`docs/PHASE_3_CHECKLIST.md`](docs/PHASE_3_CHECKLIST.md) for implementation and live-host verification status.

## Phase 4 deliverable — isolated SQL/database execution

- Separate Redis Streams database-job queue, database worker, heartbeat, per-user quotas, owner-scoped polling and queued-job cancellation.
- SQLite executes through the existing Piston/Isolate sandbox. It never executes in the process that has Docker socket access.
- MySQL 8.4 and PostgreSQL 17 run in fresh, disposable containers for each SQL job.
- Every MySQL/PostgreSQL job receives its own Docker `Internal=true` bridge network. Only the private database worker and that job's database container join it.
- Database containers have no published host ports, persistent volumes or host mounts; they use a read-only root filesystem, resource limits, reduced capabilities and no-new-privileges.
- A generated non-superuser PostgreSQL role and the image-created database-scoped MySQL learner user run setup and query SQL.
- Result rows, columns, serialized output, job time, setup statements, request sizes and concurrent jobs are bounded.
- Stale containers and networks are cleaned using CodeLeaf Runner Docker labels, including after worker interruption.
- `scripts/smoke_test_database_engines.py` verifies all three engines end-to-end through the private Runner API.

See [`docs/PHASE_4_CHECKLIST.md`](docs/PHASE_4_CHECKLIST.md), [`docs/API.md`](docs/API.md#database-execution), and [`docs/SECURITY.md`](docs/SECURITY.md#phase-4-database-execution-boundary).

## Phase 5 deliverable — production security and operations hardening

- Redis-backed per-IP HTTP request budgets, bounded request bodies, security response headers, request IDs and no-store API responses.
- Separate rate limit for interactive WebSocket session starts, exact-origin validation, and production-mode enforcement of HTTPS allowlisted origins.
- Primary/previous service-token rotation support and a distinct Docker-gateway credential.
- Explicit per-service Compose environment allowlists: only the API gets the Runner service token; the execution worker does not; only the database worker and Docker gateway get the Docker-gateway credential.
- Docker socket isolated in a restricted gateway with fixed Docker API route/image/container/network allowlists. The database worker no longer mounts the host Docker socket.
- Internal-only Docker networks, no normal host-published Piston/Redis/gateway ports, opt-in localhost-only Piston admin sidecar, resource limits, read-only filesystems and dropped Linux capabilities wherever supported.
- Worker heartbeats, readiness checks, protected Prometheus-compatible queue/worker metrics, Redis job leases, pending-job recovery and orphan database resource cleanup.
- Production preflight verifies Compose trust boundaries, distinct generated secrets, exact HTTPS origins and immutable image digests for Piston, Redis, SQL images, the admin sidecar and the Python base image.

See [`docs/PHASE_5_SECURITY_SCALING.md`](docs/PHASE_5_SECURITY_SCALING.md), [`docs/PHASE_5_CHECKLIST.md`](docs/PHASE_5_CHECKLIST.md), and [`docs/SECURITY.md`](docs/SECURITY.md). These controls are implementation safeguards, not a substitute for independent host/kernel penetration testing before public launch.

## Folder structure

```text
codeleaf-runner/
├── .dockerignore
├── .env.example
├── .gitignore
├── app/
│   ├── __init__.py
│   ├── auth.py
│   ├── config.py
│   ├── database_execution.py
│   ├── database_jobs.py
│   ├── database_models.py
│   ├── database_worker.py
│   ├── docker_engine.py
│   ├── docker_gateway.py
│   ├── security.py
│   ├── sql_validation.py
│   ├── jobs.py
│   ├── interactive.py
│   ├── main.py
│   ├── models.py
│   ├── piston.py
│   ├── registry.py
│   ├── results.py
│   ├── runtime_catalogue.py
│   ├── sessions.py
│   └── worker.py
├── config/
│   ├── languages.json
│   ├── runtime-manifest.json
│   └── runtime-smoke-tests.json
├── docs/
│   ├── API.md
│   ├── CODELEAF_INTEGRATION.md
│   ├── PHASE_1_CHECKLIST.md
│   ├── PHASE_2_CHECKLIST.md
│   ├── PHASE_3_CHECKLIST.md
│   ├── PHASE_4_CHECKLIST.md
│   ├── PHASE_5_CHECKLIST.md
│   ├── PHASE_5_SECURITY_SCALING.md
│   ├── RUNTIME_INSTALLATION.md
│   └── SECURITY.md
├── scripts/
│   ├── check-piston.sh
│   ├── install-piston-runtimes.sh
│   ├── provision_piston_runtimes.py
│   ├── smoke_test_piston_runtimes.py
│   ├── smoke_test_database_engines.py
│   ├── security_preflight.py
│   └── verify_piston_runtimes.py
├── tests/
│   ├── conftest.py
│   ├── test_api_contract.py
│   ├── test_queue_safety.py
│   ├── test_database_phase.py
│   ├── test_interactive_sessions.py
│   ├── test_registry_models.py
│   ├── test_results_auth.py
│   ├── test_runtime_catalogue.py
│   ├── test_runtime_provisioning.py
│   ├── test_runtime_smoke_tools.py
│   ├── test_docker_gateway.py
│   ├── test_security_phase.py
│   └── test_phase5_compose_security.py
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── requirements.txt
├── THIRD_PARTY_NOTICES.md
└── README.md
```

## Where to store this repository

Create a **new folder outside your existing CodeLeaf project**. On your Windows development computer, use:

```text
E:\CodeLeaf-Runner\
```

Extract the standalone CodeLeaf Runner project ZIP directly into that folder. Do not extract it inside `E:\Code_Leaf\`, and do not copy its `app/`, `config/`, or Docker files into the existing CodeLeaf frontend/backend folders.

Create a separate GitHub repository named `CodeLeaf-Runner` and push this folder as its own repository. The Piston engine is a separate container dependency. The runtime provisioning tool uses Piston's private package-management API and does not clone the Piston source tree into this repository.

## Host requirement

Piston requires Linux, Docker/Compose, and cgroup v2. Use a dedicated Ubuntu/Linux VM or server for the actual public execution node. Do not deploy Piston in the existing Render FastAPI web service, do not place it beside Neon credentials or Catalyst credentials, and do not expose Piston's port `2000` to the public Internet. Piston's own setup instructions require Docker and cgroup v2, and the standalone container runs privileged so it can configure Isolate. See [Piston setup](https://github.com/engineer-man/piston#Getting-Started) and [the security documentation](https://github.com/engineer-man/piston#Security).

Windows can be used to edit this repository. The real Piston runtime host should be Linux. Docker Desktop/WSL2 may be useful for development, but verify cgroup v2 and Isolate behavior before treating it as a valid execution host.

## Local/development setup on a Linux Docker host

1. Copy the environment template:

   ```bash
   cp .env.example .env
   ```

2. Generate a long random token:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

3. Generate a second, different random token and put it in `.env` as `RUNNER_DOCKER_GATEWAY_TOKEN`. Keep both values private; never reuse the service token for the Docker gateway.
4. Start the stack:

   ```bash
   docker compose up -d --build
   ```

5. Run the security preflight and check API readiness:

   ```bash
   python scripts/security_preflight.py
   curl http://127.0.0.1:8080/health/ready
   ```

6. Preview the core package installation plan:

   ```bash
   python3 scripts/provision_piston_runtimes.py --profile core
   ```

7. Install the selected core runtime packages and write exact version pins:

   ```bash
   python3 scripts/provision_piston_runtimes.py --profile core --apply --write-lock config/runtime-lock.json
   ```

8. Verify registry availability and execute real smoke tests:

   ```bash
   python3 scripts/verify_piston_runtimes.py --profile core
   python3 scripts/smoke_test_piston_runtimes.py --require-profile core
   ```

   For `extended`, `all`, exact-version pinning and maintenance, follow `docs/RUNTIME_INSTALLATION.md`.

9. Confirm the CodeLeaf Runner API can see the installed catalogue. Authenticated language listing is described in `docs/API.md`.

The first Piston start has no language packages installed. The API correctly returns `available: false` until a runtime is installed. Runtime installation is an administrator operation, not a learner API operation.

The database worker pre-pulls the fixed MySQL and PostgreSQL images before publishing its heartbeat, so the first learner query does not spend its execution timeout downloading an image. Image tags in `.env.example` are development defaults; pin immutable image digests before a public deployment.

## Phase 5 security preflight

After creating your private `.env` file, run the development safety checks:

```powershell
python scripts/security_preflight.py
```

For production, first configure unique service/gateway secrets, exact HTTPS WebSocket origins, and immutable image digests, then run:

```powershell
python scripts/security_preflight.py --production
docker compose config --quiet
```

The preflight never prints secret values. It checks the Docker socket boundary, internal networks, host port bindings, worker privilege, Piston network disablement, origin policy, and production image pins. See [`docs/PHASE_5_SECURITY_SCALING.md`](docs/PHASE_5_SECURITY_SCALING.md).

## Run automated checks

From a Python 3.11+ environment in the repository root:

```bash
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e ".[test]"
python -m pytest -q
```

The `pyproject.toml` explicitly limits setuptools package discovery to `app*`; this avoids the earlier flat-layout error where setuptools also discovered the top-level `config/` data directory. In an offline environment that already has setuptools and wheel installed, `python -m pip install --no-build-isolation -e ".[test]"` can be used instead.

The tests cover API and database job contracts, owner scoping, token checks/rotation, language aliases, runtime catalogue and provisioning, source limits, multi-file workspaces, interactive stdin/stdout and signals, SQL statement validation and actual database ownership contracts, Docker gateway allowlists, request/WebSocket rate limits, production startup rules, and Compose trust boundaries. They do not replace a real Linux/Piston/MySQL/PostgreSQL end-to-end security test or host/kernel penetration test.

## API endpoints

- `GET /health/live` — process liveness.
- `GET /health/ready` — Redis, Piston and service-token readiness.
- `GET /v1/languages` — installed runtime catalogue; requires `X-Runner-Key`.
- `GET /v1/runtime-status` — installed runtime/package counts and per-language availability; requires `X-Runner-Key`.
- `POST /v1/jobs` — enqueue a code job; requires `X-Runner-Key` and a CodeLeaf owner scope.
- `GET /v1/jobs/{job_id}` — retrieve a job; requires service token and matching `X-CodeLeaf-User`.
- `DELETE /v1/jobs/{job_id}` — cancel a queued job; requires service token and matching owner scope. A running REST job cannot be killed because Piston's REST endpoint is synchronous.
- `WS /v1/sessions` — start multi-file interactive execution and stream stdin/stdout/stderr; requires private service headers.
- `DELETE /v1/sessions/{session_id}` — send SIGKILL to an active interactive process; owner-scoped.
- `GET /v1/database/engines` — report SQLite/MySQL/PostgreSQL availability.
- `POST /v1/database/jobs` — enqueue a disposable SQL execution.
- `GET /v1/database/jobs/{job_id}` — retrieve an owner-scoped SQL result.
- `DELETE /v1/database/jobs/{job_id}` — cancel a queued SQL job.

The full request/response contract is in `docs/API.md`.

## Phase 3 interactive usage

See [`docs/API.md`](docs/API.md#interactive-terminal-sessions) for the full WebSocket protocol, stdin messages, process signals, session termination endpoint and limits. Interactive sessions use Piston's self-hosted `/api/v2/connect` endpoint and are tracked with owner-scoped Redis leases. The browser must connect through the CodeLeaf backend; direct browser access to the Runner service secret is prohibited.

## Phase 4 database smoke test

After `docker compose up -d --build` reports the `database-worker` running and the worker has pre-pulled its fixed database images, run:

```bash
python scripts/smoke_test_database_engines.py
```

It reads the local service token from `.env` without printing it, submits create/insert/select jobs for SQLite, MySQL and PostgreSQL, polls each job, and confirms the result is `[[1, "CodeLeaf"]]`. This checks the actual API, queue, worker, database container and cleanup path. See `docs/PHASE_4_CHECKLIST.md` for the Windows commands and expected output.

## Current scope limits

- REST `/v1/jobs` remains a one-shot execution endpoint. Interactive terminal use is provided separately by `WS /v1/sessions`.
- Piston accepts flat file names without directory paths. This release supports multi-file projects in one workspace root; nested directory trees need a later bundling strategy.
- The interactive API is private. The CodeLeaf browser must not receive the Runner service token; the eventual CodeLeaf backend must proxy the WebSocket after authenticating the learner.
- The registry contains Piston language identifiers, but only installed packages show as available. Use the live runtime catalogue and provisioning/verification scripts; do not assume all 81 entries are available in every Piston release.
- The Piston SQL package is SQLite (`sqlite3`). MySQL and PostgreSQL need their own isolated disposable database runners; they must never connect to CodeLeaf's production databases.
- Browser previews for HTML/CSS/React remain in the CodeLeaf frontend. CodeLeaf Runner handles server-side terminal execution only.
- Python scientific modules, web frameworks, and other third-party libraries depend on the packages in the selected runtime. A language being installed does not guarantee every library in the Python ecosystem is present.
- The current CodeLeaf backend is not modified by this separate-repository work. Production connection and WebSocket proxying are deliberately deferred until all six Runner phases are complete.

## Important production gate

Do not send public learner code to this service until it is deployed on an isolated Linux host, the Piston image is pinned to a verified immutable digest, the required runtimes are installed, TLS/private-network access is configured, and the end-to-end isolation checks in `docs/SECURITY.md` have been completed.
