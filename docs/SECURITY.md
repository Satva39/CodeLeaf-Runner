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
- The service token must only exist on the main CodeLeaf backend and the Runner deployment.
- Owner IDs are trusted only when supplied by the authenticated main backend. The browser must not choose them.
- Piston is not exposed through the reverse proxy. Only the CodeLeaf Runner API is reachable by the main backend.
- Redis and Piston ports must not be publicly reachable.
- Do not mount the CodeLeaf source, `.env`, Neon credentials, Catalyst credentials, or host home directory into Piston.

## Piston isolation

Piston uses Isolate with Linux namespaces, chroot, unprivileged execution users, process/time/output/file limits and network restrictions. Piston documentation says the default networking restriction is enabled and provides configurable process, output, file, compile/run time and memory limits. CodeLeaf Runner also sets explicit memory/time limits instead of relying on Piston's documented default memory setting, which can be unlimited if left unset.

Piston setup requires Docker and cgroup v2. Its standalone container requires privileged mode to set up the Isolate environment. That privileged container is the highest-risk component in this stack. Run it only on a dedicated, disposable Linux VM with a patched kernel and Docker engine. Do not run it on the same host as Render-like app secrets, production databases, or storage credentials.

Piston's isolation layer is a strong baseline, not a guarantee that container/host vulnerabilities cannot exist. Before public use, test the exact image, host kernel, cgroup setup, and runtime package builds. Consider an additional VM or stronger sandbox boundary for higher-risk production scale.

## Limits configured in this repository

- Max 10 files/job (configurable, max 25).
- Max 64 KiB/file and 128 KiB total source by default.
- Max 16 KiB standard input.
- Max 16 arguments, 256 characters each.
- Compile wall/CPU time: 10 seconds default.
- Run wall/CPU time: 3 seconds default.
- Compile memory: 256 MiB default.
- Run memory: 128 MiB default.
- Piston output cap: 16 KiB by default.
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
