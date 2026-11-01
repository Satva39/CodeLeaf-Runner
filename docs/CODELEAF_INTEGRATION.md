# Connecting the existing CodeLeaf backend

Phase 1 is a separate deployment. It does not modify the existing CodeLeaf repository. The API paths and headers were designed against the supplied backend's current implementation in `backend/app/phase5_routes.py`.

## Environment variables

On the CodeLeaf Runner host, set in its root `.env`:

```env
CODELEAF_RUNNER_SERVICE_TOKEN=<one-long-random-secret>
```

On the existing CodeLeaf Render backend, set:

```env
RUNNER_SERVICE_URL=https://<private-runner-api-host>
RUNNER_SERVICE_TOKEN=<the-same-long-random-secret>
```

`RUNNER_SERVICE_URL` is the base URL only; do not append `/v1/jobs`. The current CodeLeaf backend appends `/v1/jobs` itself.

Never define the service token in `frontend/.env`, Vercel variables, client-side JavaScript, or browser requests.

## Current backend compatibility

The supplied current CodeLeaf backend performs:

1. `POST {RUNNER_SERVICE_URL}/v1/jobs` with `X-Runner-Key` and JSON containing `owner_id`, `language`, `source`, and `stdin`.
2. It stores the returned `job_id` in its existing `RunnerJob` record.
3. It polls `GET {RUNNER_SERVICE_URL}/v1/jobs/{job_id}` with both `X-Runner-Key` and `X-CodeLeaf-User`.
4. It calls `DELETE {RUNNER_SERVICE_URL}/v1/jobs/{job_id}` with both headers for cancellation.

This Phase 1 service accepts the same request shape, uses the same endpoint paths and header names, and returns the job fields expected by that existing flow. No CodeLeaf SQLAlchemy migration is required for the Phase 1 service contract.

## Private network requirement

The Compose file binds API port `8080` and Piston admin port `2000` to host loopback only. Redis has no published host port. This is intentional. A Render service cannot reach another machine's `127.0.0.1`.

Before turning on the integration, connect Render to the runner host using a private overlay/tunnel or a carefully restricted HTTPS reverse proxy with TLS, a strong service token, and an IP/network allowlist where available. Do not publish Piston `:2000` or Redis `:6379`. The only API reachable by the CodeLeaf backend should be the CodeLeaf Runner API on `:8080` through that protected connection.

The exact private-network method depends on the Linux host/provider you select. Do not guess a public URL or weaken the firewall simply to make the first request work.

## Existing status endpoint follow-up

The current CodeLeaf `/api/runner/status` route reports a static Python/JavaScript list. It should later be updated to call this service's authenticated `GET /v1/languages` endpoint and return only `available: true` runtimes. Do that as the CodeLeaf integration phase, not by hardcoding all registry languages as executable.

## Safe rollout order

1. Deploy the Runner stack on a dedicated Linux node.
2. Install initial runtime packages using the admin script.
3. Verify `GET /health/ready` on the host and authenticated `GET /v1/languages` from a trusted machine.
4. Set `RUNNER_SERVICE_URL` and the matching `RUNNER_SERVICE_TOKEN` on Render.
5. Confirm backend logs and API job polling for a simple C++ and Python sample.
6. Only then enable the additional languages in CodeLeaf's UI based on the runner catalogue.

Do not remove JDoodle credentials yet. Keep the previous provider available as a rollback option until real Piston execution, resource limits, and runner networking have been validated in production. JDoodle removal belongs to a later integration phase.
