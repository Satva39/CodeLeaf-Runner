# Security and operational boundary

CodeLeaf Runner accepts learner-controlled source code. Treat every submitted program as hostile input, even when the student is using a lesson exercise.

## Trust boundaries

```text
Browser
  |
  v
CodeLeaf frontend
  |
  v
CodeLeaf main FastAPI backend
  |  verifies session/JWT, plan quotas and user ID
  |  sends X-Runner-Key + X-CodeLeaf-User
  v
Private CodeLeaf Runner API
  |-------------------- Redis Streams / job state
  |
  v
Private execution worker
  |
  v
Piston API (separate execution engine)
  |
  v
Isolate per submission
```

- The browser must never call CodeLeaf Runner directly.
- The service token belongs only in the main CodeLeaf backend and Runner API. The normal code worker and Docker-gateway-backed database worker do not receive it. Compose uses explicit per-service environment allowlists rather than passing the full `.env` file into application containers.
- Owner IDs are trusted only when supplied by the authenticated main backend. The browser must not choose them.
- Piston is not exposed through the reverse proxy. Only the CodeLeaf Runner API is reachable by the main backend.
- Redis and Piston ports must not be publicly reachable.
- Do not mount the CodeLeaf source, `.env`, Neon credentials, Catalyst credentials, or host home directory into Piston.

## Piston isolation

Piston uses Isolate with Linux namespaces, chroot, unprivileged execution users, process/time/output/file limits and network restrictions. Piston documentation says the default networking restriction is enabled and provides configurable process, output, file, compile/run time and memory limits. CodeLeaf Runner also sets explicit memory/time limits instead of relying on Piston's documented default memory setting, which can be unlimited if left unset.

Piston setup requires Docker and cgroup v2. Its standalone container requires privileged mode to set up the Isolate environment. That privileged container is the highest-risk component in this stack. Run it only on a dedicated, disposable Linux VM with a patched kernel and Docker engine. Do not run it on the same host as Render-like app secrets, production databases, or storage credentials.

Piston's isolation layer is a strong baseline, not a guarantee that container/host vulnerabilities cannot exist. Before public use, test the exact image, host kernel, cgroup setup, and runtime package builds. Consider an additional VM or stronger sandbox boundary for higher-risk production scale.

## Limits configured in this repository

- Max 25 files/job; each file is limited to 256 KiB and combined source to 1 MiB.
- One-shot REST stdin is limited to 64 KiB; interactive stdin is limited to 16 KiB per message and 64 KiB total.
- Interactive output is limited to 64 KiB per session, with 256 client messages per session.
- One active interactive session per learner by default; 90-second input-idle and 120-second total session limits.
- Max 16 arguments, 256 characters each.
- Compile wall/CPU time: 30 seconds default to accommodate JVM compiler startup.
- Run wall/CPU time: 3 seconds default.
- Compile memory: 512 MiB default.
- Run memory: 128 MiB default.
- Piston output cap: 64 KiB by default.
- Piston max open files: 1024 (increased from 64 to support R runtime startup).
- Per user: 2 active jobs and 8 jobs/minute by default.
- Global queued/processing Stream depth: 250 by default.
- Job state TTL: 24 hours by default.
- Piston job networking is disabled; the engine API itself still needs outbound network when an administrator installs runtime packages. Do not enable network access for learner jobs.

## Runtime package updates

- Install only packages that appear in the Piston package catalogue.
- Prefer fixed runtime versions and record them in your deployment notes.
- Scan/update the Piston image and runtime packages on a controlled schedule.
- Pin the Piston container image to an immutable digest before public production deployment. The `.env.example` `latest` value is a development convenience, not a production pin.

## Pre-public-launch validation checklist

- [ ] Dedicated Linux VM, patched kernel and Docker engine.
- [ ] cgroup v2 enabled and verified.
- [ ] Piston API port 2000 reachable only by the Runner stack and localhost admin setup.
- [ ] Redis port 6379 not exposed publicly.
- [ ] Runner API reachable only through a protected private path from the main CodeLeaf backend.
- [ ] Strong 32+ character service token; not present in frontend assets or logs.
- [ ] Piston image pinned to an immutable digest.
- [ ] Runtime catalogue contains only intended installed packages.
- [ ] Network access from a learner program tested and confirmed unavailable.
- [ ] CPU, memory, process, time, output and file caps tested against the real host.
- [ ] Timeout and compiler-error results verified through CodeLeaf frontend polling.
- [ ] Worker crash and pending Stream message recovery tested.
- [ ] Redis persistence, backup, retention and disk monitoring configured.
- [ ] Alerting and emergency kill switch tested.

The included automated tests validate API contract and payload handling; they do not establish sandbox security or replace this end-to-end validation.


## Phase 3 interactive execution controls

- The `/v1/sessions` WebSocket is authenticated with the same service token and trusted CodeLeaf user header as the REST API. Do not expose the shared token to browser JavaScript.
- Session init validates the language against the installed runtime catalogue and reuses the same file-name, per-file, total-source and argument validators as REST jobs.
- Only stdin data and a short allowlist of process signals are accepted from the client. The client cannot spoof stdout/stderr or replace the initialized program.
- Input message size, total input, message count, output bytes, per-user concurrent sessions, input-idle time and total session duration are bounded.
- A session lease is stored in Redis; the owner-scoped DELETE route sends a Redis Pub/Sub control event to the API process and the process proxy sends SIGKILL to Piston.
- Piston remains private on the internal Docker network. Its WebSocket port is not exposed publicly.
- Multi-file execution accepts flat root-level file names only. Path separators, `..`, and absolute paths are rejected.
- Piston runs submissions inside its Isolate sandbox. The host must remain dedicated, patched, cgroup-v2 enabled, and protected by host firewalling. Privileged Piston access is a high-trust boundary; do not co-locate production secrets or databases.

## Phase 4 database execution boundary

The Docker gateway is the only service that mounts `/var/run/docker.sock`. Docker socket access is effectively host-root access, so the gateway is a high-trust control-plane process and must run only on a dedicated runner host, separate from the CodeLeaf application, Neon/Catalyst storage, production databases, and credential-bearing workloads. The gateway accepts a distinct credential and exposes only a strict allowlist of fixed MySQL/PostgreSQL image, internal-network, and disposable-container operations; it has no host-published port and joins only the private `docker-control` network. The database worker does not mount the socket: it runs as UID 10001 with a read-only filesystem, 512 MiB memory and one CPU cap, a PID cap, no Linux capabilities, and `no-new-privileges`, and calls the gateway using its separate Docker-gateway credential. The API and normal Piston code worker do not receive that credential. These limits reduce exposure but do not change the fact that Docker socket access is highly privileged.

For each MySQL/PostgreSQL job, the database worker creates a dedicated Docker bridge network with `Internal=true`, attaches itself temporarily, and creates one fixed, allow-listed database image on that same network. No port is published and no persistent volume or host bind mount is attached. The network is deleted after the DB container. Separate jobs cannot discover one another through a shared SQL network. The database container has memory, CPU, PID, open-file, shared-memory, read-only-filesystem, capability and no-new-privilege restrictions.

SQLite runs through the pre-existing Piston/Isolate execution engine. It does not execute inside the Docker-socket worker.

MySQL's generated learner account is scoped to its disposable database. PostgreSQL uses a generated non-superuser role scoped to the disposable database/schema. Setup SQL is executed using this learner identity, not the bootstrap administrator. Each database is fresh and destroyed after the job, so this is suitable for exercises, not persistent projects.

The SQL lexer validates statement boundaries and rejects client-shell commands; it is not a full SQL security parser. Isolation and database permissions are the security boundary. The database worker must never accept learner-provided Docker images, commands, environment variables, connection strings, mounts, ports, or network names.

Orphan cleanup is label-based and restricted to `codeleaf.runner.managed=true` resources. It removes stale database containers and their per-job networks. Monitor Docker disk usage, job cleanup errors, and database-worker heartbeat. Do not use this design on a shared production Docker host.


## Phase 5 production security and operations hardening

Phase 5 adds the enforcement and operations details documented in [`PHASE_5_SECURITY_SCALING.md`](PHASE_5_SECURITY_SCALING.md): request-body caps, Redis-backed HTTP and WebSocket budgets, exact HTTPS WebSocket origins in production, service-token rotation, explicit per-service environment allowlists, Docker socket confinement behind the authenticated route-allowlisting gateway, no public Redis/Piston/gateway ports, worker heartbeats, protected metrics and production image-digest preflight.

Only the API receives `CODELEAF_RUNNER_SERVICE_TOKEN`. The execution worker does not need it to consume its private Redis queue. Only the Docker gateway mounts the Docker socket; the database worker is non-root and uses the gateway with a separate credential. Do not add `.env` as a blanket `env_file` to any service.

Before any public launch, run `python scripts/security_preflight.py --production`, verify the exact Docker Compose render, pin all image references to immutable digests, and perform the live host/network/sandbox validation in the Phase 5 operations guide. The source tests do not establish that a host kernel, Docker daemon or privileged Piston container is free of vulnerabilities.
