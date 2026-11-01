# Phase 1 completion checklist

## Implemented in this repository

- [x] Separate repository layout, independent of the CodeLeaf main app.
- [x] FastAPI control-plane service.
- [x] Shared secret authentication using the existing `X-Runner-Key` contract.
- [x] Owner-scoped job reads/cancel and compatibility with the existing POST `owner_id` field.
- [x] 81-entry language registry with aliases and file extensions.
- [x] Runtime availability is discovered from the self-hosted Piston runtime endpoint.
- [x] Redis Streams queue and consumer group.
- [x] Separate worker process and configurable concurrency.
- [x] Per-user active-job and jobs-per-minute limits.
- [x] Global queue depth limit.
- [x] Multi-file payload support and standard input.
- [x] Source/path/argument size and validation limits.
- [x] Piston execution adapter with explicit limits.
- [x] Terminal result normalization for stdout, stderr, compile errors, runtime errors, timeouts and returned metrics.
- [x] API routes match the current CodeLeaf runner job routes.
- [x] Dockerfile and Compose topology for API, worker, Redis and Piston.
- [x] Runtime installer and Piston health-check scripts.
- [x] Automated tests for validation, owner scoping, registry, auth and results.

## Requires your infrastructure before live execution

- [ ] Create a new GitHub repository and push this directory.
- [ ] Provision a dedicated Linux host with Docker and cgroup v2.
- [ ] Generate and configure the production service token.
- [ ] Pin and approve the Piston image digest.
- [ ] Deploy the Compose stack.
- [ ] Install the desired runtime packages using Piston's runtime catalogue.
- [ ] Establish the protected private connection from Render to the Runner API.
- [ ] Set `RUNNER_SERVICE_URL` and `RUNNER_SERVICE_TOKEN` in the CodeLeaf Render backend.
- [ ] Perform real Piston end-to-end tests and the security checklist.

Phase 1 does not claim that the project is already deployed or that compilers have been installed. This package is the complete service foundation and deployment configuration; actual execution becomes available after the dedicated host and runtime packages are configured.
