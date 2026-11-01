# CodeLeaf Runner — Phase 1

A standalone execution service for CodeLeaf. It provides a private FastAPI control plane, a durable Redis Streams queue, worker scheduling, a live Piston runtime catalogue, and a normalized terminal-result contract.

**This repository is separate from the existing CodeLeaf website/backend repository.** It does not replace or edit `Code_Leaf/`.

## Phase 1 deliverable

- Private service-to-service authentication using `X-Runner-Key`.
- Per-job owner scope using `X-CodeLeaf-User` (plus the existing CodeLeaf POST-body compatibility field `owner_id`).
- Language registry containing 81 Piston language identifiers/aliases.
- Actual runtime availability is discovered from the Piston node. A registry entry is not considered executable until its runtime is installed.
- Redis Streams queue with consumer groups, worker concurrency, pending-message recovery, queue-depth limit, per-user active-job limit, and per-user rate limit.
- Piston REST adapter with explicit compile/run CPU, wall-time, memory, file-count, input, and output bounds.
- Multi-file source support and pre-supplied standard input.
- Normalized `stdout`, `stderr`, compiler output, exit codes, runtime version, execution timing and memory fields.
- API routes compatible with the current CodeLeaf `/v1/jobs` create/poll/cancel contract.
- Docker Compose for API, worker, Redis, and private Piston engine.

## Folder structure

```text
codeleaf-runner/
├── app/
│   ├── __init__.py
│   ├── auth.py                 # Private service-token and owner-scope checks
│   ├── config.py               # Environment configuration and safety limits
│   ├── jobs.py                 # Queue, persistence, quotas and job ownership
│   ├── main.py                 # Private FastAPI endpoints
│   ├── models.py               # Strict request schemas and filename validation
│   ├── piston.py               # Piston API client
│   ├── registry.py             # Language aliases and installed-runtime matching
│   ├── results.py              # Compiler/runtime output normalization
│   └── worker.py               # Redis consumer and execution scheduler
├── config/
│   └── languages.json          # 81 known Piston language identifiers
├── docs/
│   ├── API.md
│   ├── CODELEAF_INTEGRATION.md
│   ├── PHASE_1_CHECKLIST.md
│   └── SECURITY.md
├── scripts/
│   ├── check-piston.sh
│   └── install-piston-runtimes.sh
├── tests/
│   ├── conftest.py
│   ├── test_api_contract.py
│   ├── test_registry_models.py
│   └── test_results_auth.py
├── .dockerignore
├── .env.example
├── .gitignore
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

Extract this Phase 1 ZIP directly into that folder. Do not extract it inside `E:\Code_Leaf\`, and do not copy its `app/`, `config/`, or Docker files into the existing CodeLeaf frontend/backend folders.

Create a separate GitHub repository named `CodeLeaf-Runner` and push this folder as its own repository. The Piston engine is a separate container dependency; the Piston source checkout created by the installer is placed in `vendor/piston/` and is ignored by Git.

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

3. Put the generated token into `.env` as `CODELEAF_RUNNER_SERVICE_TOKEN`.
4. Start the stack:

   ```bash
   docker compose up -d --build
   ```

5. Check API readiness:

   ```bash
   curl http://127.0.0.1:8080/health/ready
   ```

6. List the packages available from the Piston package catalogue:

   ```bash
   ./scripts/install-piston-runtimes.sh --list
   ```

   The installer downloads the official Piston CLI into the ignored `vendor/piston/` folder. It requires `git`, Node.js and npm on the admin host.

7. Install only the package identifiers that the preceding package list actually reports. For example, after confirming those exact identifiers:

   ```bash
   ./scripts/install-piston-runtimes.sh python c c++ node java php go rust bash
   ```

8. Confirm which runtimes are installed:

   ```bash
   ./scripts/check-piston.sh
   ```

9. Confirm the CodeLeaf Runner API can see the installed catalogue. Authenticated language listing is described in `docs/API.md`.

The first Piston start has no language packages installed. The API correctly returns `available: false` until a runtime is installed. Runtime installation is an administrator operation, not a learner API operation.

## Run automated checks

```bash
python -m pip install -e ".[test]"
pytest -q
```

The tests cover the API contract, owner scoping, token checks, language aliases, source-size and filename validation, and successful/compiler-error/timeout result normalization. They do not replace a real Linux/Piston end-to-end security test.

## API endpoints

- `GET /health/live` — process liveness.
- `GET /health/ready` — Redis, Piston and service-token readiness.
- `GET /v1/languages` — installed runtime catalogue; requires `X-Runner-Key`.
- `POST /v1/jobs` — enqueue a code job; requires `X-Runner-Key` and a CodeLeaf owner scope.
- `GET /v1/jobs/{job_id}` — retrieve a job; requires service token and matching `X-CodeLeaf-User`.
- `DELETE /v1/jobs/{job_id}` — cancel a queued job; requires service token and matching owner scope. A job already executing returns HTTP 409 because Phase 1 uses Piston's synchronous REST execute endpoint; live termination is a later interactive-execution feature.

The full request/response contract is in `docs/API.md`.

## Current scope limits

- Phase 1 uses Piston's REST execution endpoint. Standard input is supplied before execution; terminal streaming and typing input into a running process are not implemented yet.
- The registry contains Piston's language identifiers, but only installed packages show as available. Do not assume all 81 entries are installed.
- The Piston SQL package is SQLite (`sqlite3`). MySQL and PostgreSQL need their own isolated disposable database runners; they must never connect to CodeLeaf's production databases.
- Browser previews for HTML/CSS/React remain in the CodeLeaf frontend. CodeLeaf Runner handles server-side terminal execution only.
- Python scientific modules, web frameworks, and other third-party libraries depend on the packages in the selected runtime. A language being installed does not guarantee every library in the Python ecosystem is present.
- The current CodeLeaf backend is not modified by this separate-repository Phase 1 package. Its existing request and polling contract is supported; the exact deployment/network connection is documented separately.

## Important production gate

Do not send public learner code to this service until it is deployed on an isolated Linux host, the Piston image is pinned to a verified immutable digest, the required runtimes are installed, TLS/private-network access is configured, and the end-to-end isolation checks in `docs/SECURITY.md` have been completed.
