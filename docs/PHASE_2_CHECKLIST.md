# Phase 2 completion checklist — runtime packages

## Implemented in the repository

- [x] Live package catalogue integration based on Piston `GET /api/v2/packages`.
- [x] Piston package installation based on `POST /api/v2/packages`.
- [x] Core and extended language profiles mapped from CodeLeaf language keys to actual Piston package identifiers.
- [x] Dynamic all-profile installation of one newest available version per unique Piston package.
- [x] Safe default dry-run; installation requires `--apply`.
- [x] Installing the dynamic `all` profile requires explicit `--confirm-all`.
- [x] Existing exact package versions can be pinned through `config/runtime-lock.json`.
- [x] Lock generation records only versions confirmed by Piston and visible in the installed runtime list.
- [x] Runtime verification compares CodeLeaf's registry with the installed Piston runtime endpoint.
- [x] Smoke-test runner executes real harmless programs against installed runtimes and checks exit codes/output markers.
- [x] Authenticated `/v1/runtime-status` endpoint exposes runtime/package counts and language availability.
- [x] `/v1/languages` returns the actual installed runtime name and runtime kind.
- [x] Existing install/check shell scripts are wrappers around the new provisioning/verification tools.
- [x] Automated tests cover catalogue parsing, semantic version ordering, alias mapping, lock files, all-profile planning, runtime matching and smoke result handling.

## Requires a dedicated Linux host

- [ ] Start the Piston container on a cgroup v2 Linux host.
- [ ] Review the live Piston package catalogue.
- [ ] Install the core profile and save `config/runtime-lock.json`.
- [ ] Install the extended profile if all selected packages are present and required disk space is available.
- [ ] Review the live all-profile plan before opting into all catalogue packages.
- [ ] Run registry verification against the real Piston container.
- [ ] Run all core smoke tests against real compiler/runtime packages.
- [ ] Back up the Compose `piston_data` volume.
- [ ] Pin the tested Piston image digest and record the tested host/kernel/runtime versions.

## Deliberately deferred

- [ ] Connect the Runner to the existing CodeLeaf Render backend.
- [ ] Remove JDoodle or change existing CodeLeaf runner provider routing.
- [ ] Interactive stdin/output streaming and running-process termination.
- [ ] Isolated MySQL/PostgreSQL database execution.
- [ ] Curated scientific Python images including NumPy, Pandas, SciPy and Scikit-learn.
- [ ] Production-wide isolation audit, load testing and autoscaling.

Those tasks belong to the remaining phases. The CodeLeaf production integration remains deferred until all six phases are complete.
