# Local development and smoke testing (Windows / Docker Desktop)

Use this Compose overlay only on a developer workstation. It publishes the Runner
API on `127.0.0.1:8080` so local Python scripts can call it. It does not publish
Redis, the Piston node, the Docker gateway, or any disposable database container.

Piston package/runtime administration is exposed separately through the existing
`piston-admin` sidecar, which belongs to the `runtime-admin` Compose profile and
binds only to `127.0.0.1:2000`. Start it only while running host-side runtime
provisioning or Piston smoke tests.

## Start the local stack

Run from the repository root (`E:\CodeLeaf-Runner`):

```powershell
docker compose -f docker-compose.yml -f docker-compose.local.yml --profile runtime-admin config --quiet

docker compose -f docker-compose.yml -f docker-compose.local.yml --profile runtime-admin up -d --build redis piston api worker docker-gateway database-worker piston-admin
```

Wait for the API, Redis, Piston admin sidecar and workers to start. Then check:

```powershell
docker compose -f docker-compose.yml -f docker-compose.local.yml --profile runtime-admin ps -a
Invoke-RestMethod http://127.0.0.1:8080/health/live
Invoke-RestMethod http://127.0.0.1:2000/api/v2/runtimes
```

The first URL is the Runner API. The second URL is the temporary localhost-only
Piston admin sidecar, not a direct public Piston port.

Run host-side tests while the admin sidecar is active:

```powershell
python scripts/smoke_test_piston_runtimes.py --require-profile core --json
python scripts/smoke_test_database_engines.py
```

After package provisioning and Piston host-side smoke tests, stop the admin
sidecar. The Runner API and internal services can continue running:

```powershell
docker compose -f docker-compose.yml -f docker-compose.local.yml --profile runtime-admin stop piston-admin
```

The API remains reachable only from the local machine through `127.0.0.1:8080`.

## Production rule

Do not use `docker-compose.local.yml` in production. Deploy using the base
`docker-compose.yml` only, behind the approved private ingress/reverse proxy.
Do not publish Piston, Redis, the Docker gateway, or database containers on a
public interface. Never route `piston-admin` through a public proxy.
