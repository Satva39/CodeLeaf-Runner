# Phase 5 completion checklist

## Application security
- [x] Constant-time primary/previous service-key validation and documented rotation.
- [x] Production fail-fast for missing Docker-gateway token and missing/non-HTTPS WebSocket origins.
- [x] Exact WebSocket origin parsing; wildcard/path origins rejected.
- [x] Redis-backed per-IP REST and WebSocket connection budgets.
- [x] Request body size enforcement for declared and chunked bodies.
- [x] Security response headers, request IDs and no-store API responses.
- [x] Metrics endpoint protected by service authentication.

## Container/network isolation
- [x] Piston and Redis are not host-published in the normal stack.
- [x] Piston learner networking disabled and isolated to runner-internal.
- [x] Redis/API/execution worker are not on docker-control.
- [x] Only docker-gateway mounts the host Docker socket.
- [x] Database worker uses a narrow authenticated Docker API gateway, runs non-root, read-only and without capabilities.
- [x] Per-job SQL containers use internal networks, no ports, no host mounts and resource caps.
- [x] Piston admin port is available only through an opt-in localhost-bound profile.
- [x] API and normal worker no longer inherit the entire `.env`; only required variables are passed.

## Abuse control and reliability
- [x] Existing source/input/output/time/memory/process and queue limits retained.
- [x] Existing per-user job, database-job and interactive-session quotas retained.
- [x] Job leases and Redis pending-message recovery retained.
- [x] Worker heartbeats and readiness checks exposed.
- [x] Database orphan cleanup retained.
- [x] Redis AOF persistence configured on a named volume.

## Monitoring and deployment
- [x] Protected Prometheus text metrics for queue depths and worker heartbeats.
- [x] Security preflight script validates Compose boundaries and secrets.
- [x] Production preflight requires exact HTTPS WebSocket origins.
- [x] Production preflight requires immutable image digests for all pulled/base images.
- [x] Security, operation, rotation and deployment documentation added.
- [ ] Run the new security preflight against the operator's actual `.env` and local Docker Compose file.
- [ ] Rebuild and smoke-test the modified Docker Compose topology on the user's host.
- [ ] Deploy on a dedicated Linux production host and complete the live adversarial security test checklist before public exposure.

The last three items require the operator's actual secrets, Docker host and deployment environment. They are not claimed as completed by the source-only implementation.
