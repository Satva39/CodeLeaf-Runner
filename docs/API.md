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

No authentication. Returns `200` only when the service token is configured, Redis is reachable, and Piston's runtime catalogue is reachable. Do not expose this endpoint as a detailed public diagnostics service.

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
- A running job returns `409`. Phase 1 calls Piston's synchronous REST endpoint, which does not provide a job ID for terminating a running process. Interactive WebSocket process signaling is a later feature.

## Queue and result retention

- Redis Streams stores queued work and supports worker consumer groups.
- Redis hashes store job state/result with a configurable TTL (default 24 hours).
- Submitted source is held in Redis while queued/running and cleared from the job record after finalization.
- Job output is bounded; never use this queue as permanent source-code storage.
