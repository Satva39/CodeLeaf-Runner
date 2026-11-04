# ARM64 validation matrix

| Gate | Test | Pass means | Does not prove |
|---|---|---|---|
| Base-image manifests | `validate-base-image-manifests.ps1` | Python, Redis, admin sidecar, MySQL and PostgreSQL publish ARM64 manifests | Actual runtime behavior on ARM64 |
| Runner service images | Compose `build` with the ARM64 overlay | API, worker, Docker Gateway and database-worker build for ARM64 | Native host kernel / Docker API behavior |
| Build | `docker buildx build --platform linux/arm64 --load` | Build graph produced an ARM64 image | Native execution or runtime package support |
| Image metadata | `docker image inspect` | OCI metadata says `linux/arm64` | Every executable inside is ARM64 |
| Node binary | `node -p process.arch` in container | Node runtime is ARM64 | Isolate works with host cgroups |
| Kernel view | `uname -m` in container | Runtime sees AArch64 under ARM64 execution/emulation | Native ARM64 hardware performance |
| Isolate artifact | `file /usr/local/bin/isolate` | Isolate binary architecture can be inspected | Sandbox/cgroup execution succeeds |
| Piston API | `/`, `/api/v2/packages`, `/api/v2/runtimes` | Piston server starts and responds | Language packages are ARM64 |
| Runtime install | existing provisioner | Package installation completes | Program binary can execute |
| Runtime smoke | existing Runner smoke tests | Tested runtime programs execute through Runner | Untested runtimes or production load |
| Native host | repeat all gates on ARM64 Linux + cgroup v2 | Actual target host compatibility | Long-duration production capacity |

## Result labels

- `PASS`: the exact gate was run and passed.
- `FAIL`: the exact gate was run and failed.
- `UNVERIFIED`: not run yet.
- `UNAVAILABLE`: upstream package is not offered or cannot be built for ARM64.

Never mark the entire Runner "ARM64 compatible" based only on the Docker image build.
