# CodeLeaf Runner — ARM64 compatibility validation

## Goal

Qualify the existing CodeLeaf Runner stack for Oracle Cloud Infrastructure (OCI)
Ampere ARM64 deployment before provisioning a server.

This patch overlays ARM64 platform selection on the existing services. It does
not rewrite the FastAPI API, Redis Streams, worker, Piston adapter, Docker
Gateway, database worker, API contract, secrets, volumes, security controls,
or internal network boundaries.

## Important corrections in this package

The previous draft had two defects:
- It built Isolate in a `BUILDPLATFORM` stage, which can produce an x86-64
  executable when the host is x86-64.
- It told you to run `validate-base-image-manifests.ps1`, but that file was
  missing.

This package compiles Isolate and installs the Piston Node native dependency
in `TARGETPLATFORM` stages, includes the manifest checker, and offers one
entry-point script. v4 also makes temporary Docker container/volume cleanup
idempotent so a missing resource cannot abort the validator or mask its real
smoke-test result.

## Files

- `piston-arm64/Dockerfile` — custom Piston image from pinned upstream source.
- `scripts/check-arm64-build-prerequisites.ps1` — Docker/Buildx/cgroup checks.
- `scripts/validate-base-image-manifests.ps1` — verifies ARM64 manifests for
  Python, Redis, MySQL, PostgreSQL, Socat, Node and build dependencies.
- `scripts/build-arm64.ps1` — builds `linux/arm64` and inspects its metadata.
- `scripts/validate-arm64-image.ps1` — starts a temporary Piston container and
  verifies Node, Isolate ELF architecture and Piston HTTP endpoints.
- `scripts/verify-installed-runtime-architecture.ps1` — scans installed
  runtime package ELF files for non-AArch64 binaries.
- `scripts/oracle-native-preflight.sh` — native OCI Linux architecture,
  cgroup-v2 and Docker checks.
- `docker-compose.arm64.yml` — ARM64 overlay for all existing Runner services.
- `PATCH-INSTRUCTIONS.txt` — extraction, run, integration and rollback steps.

## 1. Extract correctly on Windows

Download the v4 ZIP, then put it in `E:\CodeLeaf-Runner` (the same folder that
contains the existing `docker-compose.yml`). Open PowerShell:

```powershell
cd E:\CodeLeaf-Runner
Expand-Archive -LiteralPath .\CodeLeaf-Runner-ARM64-Validation-v4.zip -DestinationPath . -Force
Test-Path .\CodeLeaf-Runner-ARM64-Validation\INSTALL-AND-CHECK.ps1
```

The last command must print `True`. If it prints `False`, the ZIP was not
extracted into the repository root or the archive was renamed. Do not execute
the script until that path exists.

## 2. Build and validate Piston in one command

```powershell
Set-ExecutionPolicy -Scope Process Bypass
& .\CodeLeaf-Runner-ARM64-Validation\INSTALL-AND-CHECK.ps1
```

Expected gates:
1. Docker Desktop is running Linux containers.
2. Buildx advertises `linux/arm64`.
3. Every external base/service image used in the stack has an ARM64 manifest.
4. Custom Piston builds for `linux/arm64`.
5. OCI metadata says `linux/arm64`.
6. The temporary test container reports `node process.arch=arm64`,
   `uname -m=aarch64`, and the Isolate ELF machine as `AArch64`.
7. Piston root, packages and runtimes endpoints respond.

On an x86-64 Windows PC this uses emulation. It is a build/API smoke test, not
a native ARM64 hardware or production sandbox certification.

### If Buildx does not advertise ARM64

Update Docker Desktop and enable its Linux/WSL2 backend and ARM64 emulation.
Then run `docker buildx inspect --bootstrap` and retry. Do not silently use the
official AMD64 Piston image as a fallback.

## 3. Validate the whole existing Runner stack on the local ARM64 target

The existing `.env` must already contain valid, distinct development secrets.
Do not replace or commit `.env`. The overlay keeps the existing base Compose
security controls and sets `platform: linux/arm64` on all services.

```powershell
$compose = @(
  "-f", "docker-compose.yml",
  "-f", "docker-compose.local.yml",
  "-f", "CodeLeaf-Runner-ARM64-Validation\docker-compose.arm64.yml"
)

docker compose @compose config --quiet
if ($LASTEXITCODE -ne 0) { throw "ARM64 Compose configuration is invalid." }

docker compose @compose --profile runtime-admin up -d --build redis piston api worker docker-gateway database-worker piston-admin
if ($LASTEXITCODE -ne 0) { throw "ARM64 Runner stack failed to start." }

docker compose @compose --profile runtime-admin ps -a
docker compose @compose logs --tail=80 piston api worker docker-gateway database-worker
```

Keep Piston port 2000, Redis 6379 and Docker Gateway 2375 private. The
`piston-admin` sidecar is an opt-in localhost-only maintenance path, not a
public endpoint.

## 4. Provision and test language runtimes

A successful Piston API build does NOT prove language-runtime compatibility.
Piston's package catalogue may advertise packages whose binaries were built
for a different CPU architecture.

First inspect the install plan:

```powershell
python scripts/provision_piston_runtimes.py --base-url http://127.0.0.1:2000 --profile core
```

Only then install the profile, writing a separate ARM64 lock file so that the
existing x86-64 `config/runtime-lock.json` remains untouched:

```powershell
python scripts/provision_piston_runtimes.py --base-url http://127.0.0.1:2000 --profile core --apply --write-lock config/runtime-lock.arm64.json
```

Stop the admin sidecar after provisioning:

```powershell
docker compose @compose --profile runtime-admin stop piston-admin
```

Scan package files for wrong-architecture ELF binaries:

```powershell
.\CodeLeaf-Runner-ARM64-Validation\scripts\verify-installed-runtime-architecture.ps1
```

Then run the Runner's own runtime verification and real API/worker smoke tests:

```powershell
python scripts/verify_piston_runtimes.py --profile core
python scripts/smoke_test_piston_runtimes.py --require-profile core --json
```

A runtime is compatible only when its installed executables are ARM64 and its
program tests pass through CodeLeaf Runner. Record any failure as
`UNAVAILABLE` or `UNVERIFIED`; do not mark all 81 registry languages supported.

## 5. Validate the database execution path

The MySQL and PostgreSQL official images in the repository's current tags
publish ARM64 manifests. Still verify the actual disposable-container path:

```powershell
python scripts/smoke_test_database_engines.py
```

SQLite, MySQL and PostgreSQL must each pass. This confirms that the database
worker, Docker Gateway, per-job internal Docker networks and cleanup work
together under ARM64. An image manifest alone does not prove database
execution works.

## 6. OCI native-host gate

After an OCI Ampere VM is deliberately provisioned, copy this package to the
VM and run on the VM itself:

```bash
chmod +x CodeLeaf-Runner-ARM64-Validation/scripts/oracle-native-preflight.sh
sudo CodeLeaf-Runner-ARM64-Validation/scripts/oracle-native-preflight.sh
```

It requires:
- `uname -m` to report `aarch64` or `arm64`;
- Linux cgroup v2 to be mounted;
- Docker to report ARM64 and cgroup v2;
- Docker Compose to be installed.

Then build the Piston image natively on OCI and repeat runtime and database
smoke tests on that host. Native Oracle results are the final compatibility
gate; a Windows/QEMU pass is not a substitute.

## Oracle resource notes

Oracle's published Always Free allowance for Ampere A1 is 1,500 OCPU-hours and
9,000 GB-hours per month, equivalent to 2 OCPUs and 12 GB RAM over a full
month. This is a shared tenancy allowance, not 2 OCPUs per instance. Oracle
also documents regional capacity limitations and an idle-instance reclamation
policy. Check the Always Free eligibility label and resource limits in the
OCI Console immediately before creating anything.

A 2 OCPU / 12 GB Always Free VM is a constrained pilot host, not a generous
production capacity target. Before public launch, tune Piston concurrency,
worker concurrency, database-worker concurrency and container limits to the
actual VM size and run load tests. Avoid assuming the current development
defaults are suitable for 2 OCPUs.

## OCI network/security requirements

- Use an ARM64 Ubuntu or Oracle Linux image and verify cgroup v2 after boot.
- Permit SSH only from your trusted admin IP range.
- Expose only the intended HTTPS reverse proxy (443; and 80 only for
  certificate issuance/redirect) to the Internet.
- Never expose Piston `2000`, Redis `6379`, Docker Gateway `2375`, or Runner
  internal API `8080` directly.
- Keep secrets in an untracked `.env` or an appropriate secrets manager.
- The repository's Phase 5 notes describe Piston as the highest-risk
  privileged component. For stronger isolation, plan a separate Piston VM
  from the Docker Gateway/database-worker host; this needs private
  authenticated cross-host networking and is not enabled automatically by
  this patch.

## Rollback

The patch does not modify existing project files. To stop the ARM64 stack:

```powershell
docker compose -f docker-compose.yml -f docker-compose.local.yml -f CodeLeaf-Runner-ARM64-Validation\docker-compose.arm64.yml down
```

Remove only the custom test image when no longer needed:

```powershell
docker image rm codeleaf-piston:arm64-validation
```

Do not remove `piston_data`, `redis_data`, or database volumes as part of
routine rollback. Do not delete the existing x86-64 runtime lock file.
