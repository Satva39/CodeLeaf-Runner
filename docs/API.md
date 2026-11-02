# CodeLeaf Runner API contract

All `/v1/*` routes require:

```http
X-Runner-Key: <shared-service-secret>
```

The value must match `CODELEAF_RUNNER_SERVICE_TOKEN` on this service and `RUNNER_SERVICE_TOKEN` in the CodeLeaf backend. Compare the token as a secret; never send it to the browser.

Job reads and cancellation additionally require:

```http
X-CodeLeaf-User: <authenticated CodeLeaf user ID>
```

The CodeLeaf backend must derive this value from its verified session/JWT. Never accept it directly from a browser request. For backward compatibility with the current CodeLeaf backend, `POST /v1/jobs` also accepts `owner_id` in JSON when `X-CodeLeaf-User` is absent. It is trusted only because the route is protected by the private service token and the current main backend constructs the body using its authenticated `user.id`. New integrations should use the header.

## `GET /health/live`

No authentication. Returns liveness only.

## `GET /health/ready`

No authentication. Returns `200` only when the service token is configured, Redis and Piston are reachable, and the execution worker heartbeat is present. When database execution is enabled, the database worker heartbeat is required too. Do not expose this endpoint as a detailed public diagnostics service.

## `GET /metrics`

Requires `X-Runner-Key`. Returns Prometheus text metrics for the code/database queue depths and execution/database worker heartbeat state. It deliberately excludes job IDs, source code, SQL, user IDs and secrets. Restrict scraping to your private monitoring network.

## `GET /v1/languages`

Requires `X-Runner-Key`.

Example shape:

```json
{
  "provider": "codeleaf-runner",
  "engine": "piston",
  "count": 81,
  "available_count": 3,
  "languages": [
    {
      "key": "cpp",
      "name": "C++",
      "category": "compiled",
      "extension": "cpp",
      "piston_language": "c++",
      "available": true,
      "installed_version": "<installed-version>",
      "aliases": ["cpp", "c++", "cxx"]
    }
  ]
}
```

The example version and installed count are illustrative, not a claim about a newly installed server. The real API values are read from Piston at request time.

## `GET /v1/runtime-status`

Requires `X-Runner-Key`. Returns operational counts for the live Piston runtime list and package catalogue, plus each registered CodeLeaf language's installed/available state and installed version. It never exposes Piston package-management operations to learners.

Example shape (counts and versions are live values, not fixed guarantees):

```json
{
  "provider": "codeleaf-runner",
  "engine": "piston",
  "registry_language_count": 81,
  "installed_language_count": 19,
  "installed_runtime_count": 19,
  "package_catalogue_version_count": 120,
  "installed_package_version_count": 19,
  "languages": []
}
```

Runtime installation is an administrator-only operation documented in [`RUNTIME_INSTALLATION.md`](RUNTIME_INSTALLATION.md).

## `POST /v1/jobs`

Requires `X-Runner-Key` and either `X-CodeLeaf-User` or the compatibility JSON `owner_id`.

Legacy single-file request compatible with the current CodeLeaf backend:

```json
{
  "owner_id": "<authenticated-user-id>",
  "language": "cpp",
  "source": "#include <iostream>\nint main(){ std::cout << \"Hello CodeLeaf\"; }",
  "stdin": ""
}
```

Multi-file request:

```json
{
  "language": "java",
  "version": "*",
  "files": [
    {"name": "Main.java", "content": "class Main { public static void main(String[] args) { System.out.println(\"Hello\"); } }"},
    {"name": "Helper.java", "content": "class Helper {}"}
  ],
  "stdin": "",
  "args": []
}
```

The request must provide exactly one of `source` or `files`. Files are flat root-level file names only; directory paths and traversal are rejected. Limits are configured server-side. The requested language must be in the registry and its runtime must actually be installed in Piston. A missing runtime returns HTTP 409; it is not silently routed to another provider.

Accepted response (`202`):

```json
{
  "job_id": "<uuid>",
  "status": "queued",
  "created_at": "<UTC ISO timestamp>"
}
```

## `GET /v1/jobs/{job_id}`

Requires `X-Runner-Key` and matching `X-CodeLeaf-User`.

Example response:

```json
{
  "job_id": "<uuid>",
  "status": "completed",
  "created_at": "<UTC ISO timestamp>",
  "updated_at": "<UTC ISO timestamp>",
  "result": {
    "provider": "codeleaf-runner",
    "engine": "piston",
    "outcome": "success",
    "stdout": "Hello CodeLeaf",
    "stderr": "",
    "output": "Hello CodeLeaf",
    "exit_code": 0,
    "compile_exit_code": 0,
    "runtime_language": "c++",
    "runtime_version": "<installed-version>",
    "compile_time_ms": 100,
    "run_time_ms": 2,
    "cpu_time_ms": 50,
    "memory_bytes": 8000000,
    "duration_ms": 140
  }
}
```

Metric values are illustrative. Actual results preserve Piston's compile and run values when the installed Piston build returns them. `outcome` is one of `success`, `compile_error`, `runtime_error`, `timeout`, `rejected`, or `engine_error`. A job can have HTTP job status `completed` while its program outcome is `compile_error`; this means the execution request finished and returned a real compiler result.

`404` is returned for unknown jobs and jobs belonging to another user to avoid revealing their existence.

## `DELETE /v1/jobs/{job_id}`

Requires `X-Runner-Key` and matching `X-CodeLeaf-User`.

- A queued job can be marked `cancelled`.
- A completed/failed/cancelled job returns its existing status.
- A running REST job cannot be terminated after it has entered Piston because the one-shot REST endpoint is synchronous. Use an interactive session for live process control.

## Interactive terminal sessions

The interactive route is private service-to-service infrastructure. The browser must **not** receive `X-Runner-Key`. The eventual CodeLeaf backend must authenticate the learner and proxy the WebSocket to this service with the service token and `X-CodeLeaf-User` header.

### `WS /v1/sessions`

Handshake headers:

```http
X-Runner-Key: <shared-service-secret>
X-CodeLeaf-User: <authenticated-user-id>
```

The first client message initializes one program. Supply exactly one of `source` or `files`:

```json
{
  "type": "init",
  "language": "python",
  "version": "*",
  "files": [
    {"name": "main.py", "content": "from helper import message\nprint(message())"},
    {"name": "helper.py", "content": "def message(): return 'Hello CodeLeaf'"}
  ],
  "args": []
}
```

The service validates the language against the live installed Piston runtime list, applies server-side compile/run limits, and connects to Piston's self-hosted `/api/v2/connect` WebSocket. Initialization `stdin` is intentionally not accepted; send input after the process starts.

After initializing Piston, CodeLeaf Runner sends a service envelope:

```json
{"type":"session","session_id":"<uuid>"}
```

It then forwards Piston `runtime`, `stage`, `data`, `exit`, and `error` messages. Output `data` events have `stream` = `stdout` or `stderr`.

Send interactive input (include a newline when the program expects Enter):

```json
{"type":"data","stream":"stdin","data":"Ada\n"}
```

Send a process signal:

```json
{"type":"signal","signal":"SIGINT"}
```

Accepted signals are `SIGINT`, `SIGTERM`, and `SIGKILL`. `SIGKILL` ends the session. Client messages are limited to stdin data and process signals; clients cannot write to stdout/stderr or send a second init message.

### `DELETE /v1/sessions/{session_id}`

Requires `X-Runner-Key` and matching `X-CodeLeaf-User`. It publishes a private Redis control event to the API process holding the session, which sends `SIGKILL` to Piston. A session owned by another user returns `404`.

### Server-side limits

- Up to 25 flat root-level files per execution; Piston treats the first file as the main file.
- 256 KiB per source file and 1 MiB total source.
- 16 KiB per stdin WebSocket message, 64 KiB total stdin, and 64 KiB streamed output.
- 256 client messages per session.
- One concurrent interactive session per learner by default; 90-second input-idle limit and 120-second maximum session duration.
- File paths and directory traversal are rejected. Piston's API requires filenames without directory paths; nested project trees need a later packaging strategy and are not silently flattened.

## Queue and result retention

- Redis Streams stores queued work and supports worker consumer groups.
- Redis hashes store job state/result with a configurable TTL (default 24 hours).
- Submitted source is held in Redis while queued/running and cleared from the job record after finalization.
- Job output is bounded; never use this queue as permanent source-code storage.

## Database execution

Database exercises use a separate Redis queue and worker; they do not use CodeLeaf's production database.

### `GET /v1/database/engines`

Requires `X-Runner-Key`. Returns engine availability and execution mode for SQLite, MySQL and PostgreSQL.

### `POST /v1/database/jobs`

Requires `X-Runner-Key` and the trusted `X-CodeLeaf-User` header.

```json
{
  "engine": "postgresql",
  "setup_sql": "CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT); INSERT INTO people VALUES (1, 'Ada');",
  "query": "SELECT id, name FROM people;"
}
```

The `setup_sql` field is an optional batch of at most 30 setup statements. `query` must contain exactly one SQL statement. SQL shell commands are rejected. The service returns `202 Accepted` and a `job_id`.

### `GET /v1/database/jobs/{job_id}`

Requires the same service token and owner header. Jobs belonging to another owner return `404`.

A completed result contains `engine`, `columns`, `rows`, `row_count`, `affected_rows`, `execution_ms`, `truncated` and `error`.

### `DELETE /v1/database/jobs/{job_id}`

Cancels a queued job. A running database job returns `409`; it cannot be cancelled through this queue endpoint. The database worker enforces a short hard job timeout and always removes the disposable database resources in its cleanup path.

### Engine isolation model

- SQLite runs inside the Piston/Isolate runtime and is in-memory for the job.
- MySQL and PostgreSQL run in fresh disposable containers. A new Docker `internal` network is created for every job. The private database-worker joins that network long enough to connect to the database; no host ports or persistent volumes are created.
- Results are bounded by maximum rows, columns and serialized bytes. Database errors are returned to the learner with the job result.
- Every container and its per-job network are deleted after execution. Orphan cleanup uses Docker labels to remove resources left by a worker crash.

No endpoint in this phase accepts a production database connection string, image name, Docker command, host mount, or arbitrary network configuration from the caller.

## Phase 5 operational protections

- HTTP security headers, request IDs, bounded request bodies and Redis-backed per-IP throttling.
- Separate WebSocket connection throttling and exact allowed-origin validation. Production mode requires at least one HTTPS origin.
- Service-key rotation using primary and previous secrets. The Docker-gateway secret is separate.
- `/health/ready` checks worker heartbeats; `/metrics` is authenticated and contains only queue/worker gauges.
- The Docker socket is mounted only by the narrow allowlisted Docker gateway. The database worker uses an authenticated API and does not mount the socket.
- See [`PHASE_5_SECURITY_SCALING.md`](PHASE_5_SECURITY_SCALING.md) for network topology, production image pinning, secret rotation, monitoring and incident-response checks.
