# Phase 2 — Piston runtime installation and verification

Phase 2 adds a repeatable, administrator-only runtime provisioning workflow. It does not change the public learner execution API and it does not connect this service to the main CodeLeaf backend. That connection is intentionally deferred until all six phases are complete.

## What is installed

Piston starts with **zero language runtimes**. The runtime package catalogue is the source of truth; CodeLeaf does not assume that a registry entry is installed simply because it exists in `config/languages.json`.

The included profiles are:

- `core`: the main CodeLeaf languages (C, C++, Python, JavaScript/Node, Java, C#, Go, Rust, PHP, Ruby, Kotlin, Swift, Dart, R, Bash, Lua, Perl, Scala and SQLite).
- `extended`: all languages represented in the current CodeLeaf Runner registry, subject to availability in the live Piston package catalogue.
- `all`: every unique language package advertised by the live Piston package catalogue. This can be a large install and requires explicit confirmation.

The `all` profile is dynamic. It installs the newest available version of each unique package returned by the running Piston node rather than assuming a hardcoded number of packages exists.

The runtime manifest maps CodeLeaf language keys to Piston package names. For example, CodeLeaf JavaScript maps to the Piston `node` package; after installation, Piston reports the runtime as `javascript`. SQLite exercise execution maps to the `sqlite3` package. If a package name is not in the live package catalogue, the installer reports it as missing and exits unsuccessfully instead of pretending it was installed.

## Host requirements

Run provisioning from the dedicated Linux runner host, not from Render and not from the CodeLeaf frontend/backend deployment.

Required:

- Linux host with cgroup v2 enabled and cgroup v1 disabled.
- Docker Engine and Docker Compose v2.
- Python 3.11 or newer on the administrator host (the provisioning script uses only the Python standard library).
- The CodeLeaf Runner repository and its `.env` file on that host.
- Piston's internal admin endpoint exposed temporarily through the opt-in `piston-admin` profile at `http://127.0.0.1:2000` from the host. It is not published by the normal stack.

The direct package-management API means the administrator host does not need the Piston CLI or Node.js installed. Piston itself still runs its own runtime engine in its container. Piston's upstream setup guide documents Docker, Compose and cgroup v2 requirements; its runtime API documents the package listing and installation routes. See [Piston setup](https://github.com/engineer-man/piston#Getting-Started) and [Piston API v2](https://github.com/engineer-man/piston/blob/master/docs/api-v2.md).

## Start the Piston node

From the repository root on the Linux host:

```bash
cp .env.example .env
# Generate a long secret and set CODELEAF_RUNNER_SERVICE_TOKEN in .env.
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'

docker compose up -d piston redis api worker
docker compose ps
curl --fail http://127.0.0.1:2000/api/v2/runtimes
curl --fail http://127.0.0.1:2000/api/v2/packages
```

The first runtime-list response is expected to be empty when no packages have been installed. The packages endpoint should return the list of package versions available to this Piston build and indicate whether each version is installed.

Piston's port is not published by the normal stack. The `piston-admin` sidecar is an opt-in maintenance profile that binds only to host loopback. Stop it immediately after provisioning with `docker compose --profile runtime-admin stop piston-admin`. Do not publish port 2000 on a public interface or route it through the public reverse proxy. Runtime installation is an administrator operation.

## Preview the core installation plan

The script is safe by default. Without `--apply`, it only prints a plan and performs no package installation:

```bash
python3 scripts/provision_piston_runtimes.py --profile core
```

It reads `GET /api/v2/packages`, selects the highest available version for each profile package, and prints unavailable mappings. If a package is absent from the live catalogue, the command returns a non-zero status so it cannot be mistaken for a complete installation.

To install the core runtime profile:

```bash
python3 scripts/provision_piston_runtimes.py --profile core --apply --write-lock config/runtime-lock.json
```

Package downloads/installation happen serially. Keep the process running until it completes. The exact versions confirmed by Piston are written to `config/runtime-lock.json`; this file contains public version identifiers, not credentials, and should be committed to the separate CodeLeaf-Runner repository after review.

## Add the remaining registered runtimes

Preview the extended profile:

```bash
python3 scripts/provision_piston_runtimes.py --profile extended
```

Install it:

```bash
python3 scripts/provision_piston_runtimes.py --profile extended --apply --write-lock config/runtime-lock.json
```

The extended profile tries every language mapping present in the manifest. If a runtime is absent from the live catalogue, it is listed as missing; no replacement runtime is silently substituted.

## Install every package exposed by Piston

This includes all unique package names in the live Piston package catalogue, not only languages currently included in the CodeLeaf registry. It can consume substantial disk space and may take considerable time.

Preview first:

```bash
python3 scripts/provision_piston_runtimes.py --profile all
```

Install only after reviewing the proposed package count and disk capacity:

```bash
python3 scripts/provision_piston_runtimes.py --profile all --apply --confirm-all --write-lock config/runtime-lock.json
```

The `--confirm-all` flag is mandatory for this profile when `--apply` is set.

## Pin versions for repeatable provisioning

The installer uses the newest package version advertised by Piston unless a lock file is supplied. To reinstall exactly the locked versions on another runner host:

```bash
python3 scripts/provision_piston_runtimes.py --profile core --lock-file config/runtime-lock.json --apply
```

When adding more runtimes, review the existing lock, install the desired profile and refresh the lock:

```bash
python3 scripts/provision_piston_runtimes.py --profile extended --lock-file config/runtime-lock.json --apply --write-lock config/runtime-lock.json
```

If a pinned version no longer exists in the current package catalogue, provisioning stops for that package and reports it; it does not fall forward silently.

## Verify what is really installed

Compare the local CodeLeaf registry with the live installed runtime endpoint:

```bash
python3 scripts/verify_piston_runtimes.py --profile core
python3 scripts/verify_piston_runtimes.py --profile extended
python3 scripts/verify_piston_runtimes.py --profile all --json
```

Run actual harmless `Hello CodeLeaf` programs through Piston:

```bash
python3 scripts/smoke_test_piston_runtimes.py
python3 scripts/smoke_test_piston_runtimes.py --require-profile core
```

The smoke test only runs languages installed on the node. With `--require-profile core`, it fails if any core runtime is absent. A passing smoke test means the tested programs compiled/executed and returned their expected marker; it is not a substitute for the security validation in `docs/SECURITY.md`.

## Check from the CodeLeaf Runner API

After the API/worker services are running, use a private authenticated request from the runner host:

```bash
curl --fail \
  -H "X-Runner-Key: $CODELEAF_RUNNER_SERVICE_TOKEN" \
  http://127.0.0.1:8080/v1/languages

curl --fail \
  -H "X-Runner-Key: $CODELEAF_RUNNER_SERVICE_TOKEN" \
  http://127.0.0.1:8080/v1/runtime-status
```

`available: true` means an installed Piston runtime matches that CodeLeaf registry entry. `available: false` means the corresponding runtime is not installed or is not exposed by this Piston build. The API does not infer support from the file extension or from the CodeLeaf language list.

## Runtime and library boundaries

- Piston package support and installation are driven by Piston's live package catalogue. A CodeLeaf registry label alone does not guarantee a package exists.
- JavaScript/Node is represented by Piston's `node` package and surfaced as the `javascript` runtime.
- SQL at this phase is SQLite through Piston's `sqlite3` runtime. MySQL and PostgreSQL require separate disposable database services and are not included here.
- Python's standard library is available with its Python runtime. NumPy, Pandas, SciPy and Scikit-learn are not promised by installing Python alone; they require a separately maintained, versioned scientific-Python runtime image/package strategy.
- Web frameworks and arbitrary third-party package downloads are not enabled during student execution. Do not enable learner network access to make `pip install`, `npm install`, or similar commands work.
- Docker, Git, Linux commands, Excel and YAML are not automatically programming-language runtimes just because they appear in a learning catalogue.

## Persistence and maintenance

Piston runtime packages live in the Compose `piston_data` volume mounted at `/piston`. Removing this volume removes installed packages. Back it up before host replacement or Piston upgrades. Keep the runtime lock file with the repository and record the Piston image digest used by each deployed environment.

Runtime packages should be updated deliberately: review package versions, install in a maintenance window, rerun the verification and smoke tests, and keep a rollback/backup plan. Do not automatically update the compiler fleet every time the Piston container restarts.

## Phase boundary

This phase installs and verifies server-side language runtimes. It does **not** connect the new service to the existing CodeLeaf backend, change the CodeLeaf website, remove JDoodle, add interactive stdin streaming, add disposable MySQL/PostgreSQL databases, or provide the complete security/scale work planned for later phases. The CodeLeaf production connection remains deferred until all six phases are complete, as requested.


## Phase 3 recommended Piston limits

The default Compose configuration now uses a 30-second compile wall/CPU allowance and 512 MiB compile memory to accommodate Kotlin/Scala compilation, while keeping execution time and runtime memory constrained. `PISTON_MAX_OPEN_FILES` is set to 1024 because R's runtime can exceed the former 64-descriptor value. The core package manifest also maps C and C++ to the `gcc` package and C# to `dotnet` when those package identifiers are present in the live Piston catalogue.

After updating the repository, make sure your existing `.env` values do not override the newer settings. Recreate Piston so it reads the new environment: `docker compose up -d --force-recreate piston api worker`. Re-run the core provisioning script and the live smoke test; do not mark runtimes available based solely on the package manager's catalogue.
