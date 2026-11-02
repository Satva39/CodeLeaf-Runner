# Phase 5 — Production security, abuse controls, observability and recovery

Phase 5 hardens the standalone CodeLeaf Runner service. It does not connect the existing CodeLeaf application; that remains Phase 6.

## 1. Trust boundaries

- The main CodeLeaf backend is the only intended caller of Runner REST/WebSocket APIs. Do not put `CODELEAF_RUNNER_SERVICE_TOKEN` in browser code.
- Only `api` receives the Runner service token. `worker` consumes Redis jobs but does not receive the service token. `database-worker` receives only the Redis/Piston/database settings it needs plus the separate Docker-gateway token.
- Compose uses explicit per-service environment allowlists. Do not add `env_file: .env` to application services.
- Only `docker-gateway` mounts `/var/run/docker.sock`. It is a narrow authenticated Docker Engine proxy with an allowlist for the fixed MySQL/PostgreSQL image, network and container templates.
- `database-worker` runs as UID 10001, has a read-only root filesystem, no Linux capabilities, `no-new-privileges`, resource limits, and talks to Docker through `docker-gateway`.
- `api`, `worker`, `piston`, and Redis do not join `docker-control`. The Docker gateway and database worker are the only members of that internal network.
- Piston is privileged because its Isolate/cgroup setup requires it. Treat Piston as the highest-risk component and run the complete stack on a dedicated Linux VM with cgroup v2. Do not co-locate production databases, application credentials, personal files, or unrelated workloads on that host. The current Compose topology is a hardened single-host development/initial deployment topology; for a higher-assurance public deployment, put the privileged Piston node on a separate VM from the Docker-socket database gateway/control host and connect them only over a private authenticated network. That cross-host split requires deployment-specific networking and is not claimed as automatically provisioned by this Phase 5 package.

## 2. Network and container controls

- `runner-internal` and `docker-control` are Docker `internal: true` networks.
- Redis, Piston, and Docker gateway have no published host ports in the normal stack.
- Runner API binds to `127.0.0.1:8080` by default. Place an authenticated private reverse proxy in front of it when deploying remotely.
- `piston-admin` is opt-in under the `runtime-admin` profile and binds only `127.0.0.1:2000`. Start it only for controlled package provisioning, then stop it immediately. Do not expose Piston's package-management API through a public proxy.
- API, execution worker, database worker, and gateway use resource caps, read-only filesystems where supported, dropped capabilities, `no-new-privileges`, and bounded temporary files.
- Piston learner networking is explicitly disabled. Database containers are attached only to one private per-job internal network, with no host port, volume, bind mount or external network.

## 3. Authentication and secret rotation

- REST endpoints under `/v1/` and the metrics endpoint require `X-Runner-Key`.
- Owner-scoped operations also require `X-CodeLeaf-User`. The ID must be supplied by the authenticated CodeLeaf backend, never chosen directly by a learner.
- Key checks use constant-time comparison. A previous key can be configured during a planned rotation using `CODELEAF_RUNNER_PREVIOUS_SERVICE_TOKEN`.
- Rotation procedure: set a new random `CODELEAF_RUNNER_SERVICE_TOKEN`, set the old value as `CODELEAF_RUNNER_PREVIOUS_SERVICE_TOKEN`, update the CodeLeaf backend caller, verify traffic, then clear the previous token and restart the API.
- `RUNNER_DOCKER_GATEWAY_TOKEN` is a different secret. Rotate it by updating the gateway and database-worker environment together, then recreate those services. Do not reuse the service token.
- Generate secrets with a cryptographically secure generator, for example `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Do not paste tokens into chat, screenshots, Git, issue trackers or logs.

## 4. Request and execution abuse controls

- HTTP request bodies are bounded by `RUNNER_MAX_API_BODY_BYTES`; both `Content-Length` and streamed/chunked bodies are checked.
- `/v1/` requests use a Redis-backed fixed-window per-client-IP limit. The IP is hashed before storage, and untrusted `X-Forwarded-For` headers are not accepted as identity.
- New interactive WebSocket sessions have a separate per-IP connection budget (`RUNNER_WEBSOCKET_CONNECTIONS_PER_MINUTE_PER_IP` (default 600 per ingress IP/minute)) and a per-user concurrent session limit.
- In production, the API refuses to start unless at least one exact HTTPS origin is configured in `RUNNER_ALLOWED_WEBSOCKET_ORIGINS`. Wildcards, paths, query strings and credentials in origins are rejected. Backend-to-backend WebSocket clients that omit `Origin` still require the service token and owner header.
- Code execution is bounded by source-file count/size, stdin, args, compile/run time, CPU, memory, output, processes, active jobs per user, jobs per minute and queue depth.
- Database execution has separate per-user concurrency/rate limits, query/setup byte limits, query/job timeouts, bounded result rows/bytes, and disposable database containers.
- Rate limiting fails closed with HTTP 503 if Redis is unavailable; it does not silently turn off abuse protection.

## 5. Queue reliability and cleanup

- Each job claim is protected by a Redis lease. A worker that sees another live lease does not acknowledge the stream message.
- Workers publish short-lived heartbeats. `/health/ready` reports worker readiness, not merely API process liveness.
- Redis Streams pending-message recovery uses `XAUTOCLAIM` after the corresponding lease has had time to expire.
- Database worker cleanup periodically removes stale CodeLeaf-labeled database containers and per-job networks. It never cleans resources without the managed labels.
- Redis AOF persistence is enabled on a named volume. Back up the volume using a tested Redis backup procedure and periodically test restore. Monitor disk usage; AOF does not replace backups.
- Job hashes have finite TTLs. Do not persist learner source code or execution results indefinitely.

## 6. Monitoring

- `GET /health/live` is a process liveness check.
- `GET /health/ready` checks Redis, Piston, the execution worker heartbeat, and the database worker heartbeat when database execution is enabled.
- `GET /metrics` requires `X-Runner-Key` and returns Prometheus text metrics for code/database queue depth and worker heartbeats. It intentionally does not expose job IDs, source code, SQL, tokens, or learner identifiers.
- Collect container CPU/memory/PID usage, Docker disk usage, Redis memory/AOF status, Piston errors/timeouts, queue depth, worker heartbeat loss, rate-limit responses, and orphan-cleanup warnings in the host monitoring system.
- Configure alerts for worker heartbeat loss, queue growth, Docker disk pressure, repeated execution failures, gateway authentication failures, and cleanup failures.

## 7. Image and deployment supply-chain checks

For production, set `RUNNER_PRODUCTION_MODE=true` and pin every externally pulled image used by this stack to an immutable digest:

- `PISTON_IMAGE`
- `REDIS_IMAGE`
- `RUNNER_DATABASE_MYSQL_IMAGE`
- `RUNNER_DATABASE_POSTGRES_IMAGE`
- `PISTON_ADMIN_IMAGE` (opt-in maintenance sidecar)
- `RUNNER_PYTHON_BASE_IMAGE` (base image used to build application containers)

An image reference should end with `@sha256:` followed by 64 lowercase hexadecimal characters. Update digests deliberately, rebuild, run the complete test suite and live smoke tests, then deploy. Do not treat mutable `latest` or floating tags as production pins.

Run the preflight before building:

```powershell
python scripts/security_preflight.py
python scripts/security_preflight.py --production
```

The production check verifies secret presence/distinctness without printing values, exact HTTPS origins, immutable image digests, and the Docker Compose trust boundaries. It does not replace Docker host, kernel, network, or application penetration testing.

Then validate and deploy:

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps -a
Invoke-RestMethod http://127.0.0.1:8080/health/ready
```

To expose Piston temporarily for package installation on the host:

```powershell
docker compose --profile runtime-admin up -d piston-admin
# Run the documented runtime provisioning/verification commands.
docker compose --profile runtime-admin stop piston-admin
```

## 8. Production-mode environment

At minimum, configure these values in the private Runner `.env` file:

```dotenv
RUNNER_PRODUCTION_MODE=true
CODELEAF_RUNNER_SERVICE_TOKEN=<unique generated secret, 48+ random bytes>
RUNNER_DOCKER_GATEWAY_TOKEN=<different unique generated secret, 48+ random bytes>
RUNNER_ALLOWED_WEBSOCKET_ORIGINS=https://your-approved-codeleaf-origin.example
PISTON_IMAGE=ghcr.io/engineer-man/piston@sha256:<64-hex-digest>
REDIS_IMAGE=redis@sha256:<64-hex-digest>
RUNNER_DATABASE_MYSQL_IMAGE=mysql@sha256:<64-hex-digest>
RUNNER_DATABASE_POSTGRES_IMAGE=postgres@sha256:<64-hex-digest>
PISTON_ADMIN_IMAGE=alpine/socat@sha256:<64-hex-digest>
RUNNER_PYTHON_BASE_IMAGE=python@sha256:<64-hex-digest>
```

Replace every example with a real digest for the exact architecture and image you deploy. Never copy these example values verbatim. The main CodeLeaf integration variables remain deferred until Phase 6.

## 9. Validation boundary

Automated unit/contract tests validate the policy and code paths, not the security of the host kernel or Docker daemon. Before public use, independently test container escape boundaries, DNS/egress denial, process and memory exhaustion, file and output caps, WebSocket origin enforcement, token rotation, rate limits under concurrency, worker crashes, database orphan cleanup, backups/restores, and incident response. A privileged Piston instance and any process with Docker-socket access remain high-trust components despite these controls.
