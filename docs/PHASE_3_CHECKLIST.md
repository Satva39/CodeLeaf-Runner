# Phase 3 completion checklist — multi-file, interactive terminal and process control

## Implemented in this repository

- [x] REST job requests accept up to 25 flat root-level files and preserve the submitted file order (first Piston file is the main file).
- [x] Per-file source limit increased to 256 KiB and aggregate source limit to 1 MiB, with server-side enforcement.
- [x] Existing one-shot `stdin` execution remains supported by `POST /v1/jobs`.
- [x] Private `WS /v1/sessions` route proxies Piston's self-hosted `/api/v2/connect` protocol.
- [x] Interactive init validates language, installed runtime version, files, args and source limits before opening a Piston process.
- [x] Interactive stdin streams to the running program; stdout and stderr stream back as Piston events.
- [x] Compile/run stage and exit events are forwarded to the client.
- [x] Client process signals are restricted to SIGINT, SIGTERM and SIGKILL.
- [x] Owner-scoped `DELETE /v1/sessions/{session_id}` sends SIGKILL through Redis Pub/Sub.
- [x] Redis session leases enforce one concurrent interactive session per learner by default and expire if an API process crashes.
- [x] Input size, output bytes, message count, idle time, total duration and concurrent sessions are bounded server-side.
- [x] No service token is intended for the browser; the future CodeLeaf backend must proxy the WebSocket after verifying the learner.
- [x] Piston package candidates now map C/C++ to GCC and C# to dotnet when present in the package catalogue.
- [x] Compile time/memory settings and Piston open-file limit are raised for real Kotlin/Scala/R runtime startup while runtime execution remains constrained.
- [x] Automated tests cover init validation, multi-file requests, streaming, stdin, process events, session quota and owner-scoped cancellation.

## Requires live-host verification

- [ ] Recreate the Piston container so updated compile/open-file settings apply.
- [ ] Install `gcc` and `dotnet` package mappings if available from the live Piston package catalogue, then verify C/C++/C# runtimes appear in `/api/v2/runtimes`.
- [ ] Re-run the live core smoke suite and resolve any remaining Kotlin/Scala/R failures.
- [ ] Run multi-file Java/Python execution against the actual Piston container.
- [ ] Run an interactive `input()` / `readLine()` program and confirm stdin/stdout streaming.
- [ ] Test SIGKILL through both WebSocket signal and owner-scoped DELETE endpoint.
- [ ] Verify Piston process and session cleanup after disconnect, idle timeout and output-limit termination.
- [ ] Perform security and load testing on the dedicated Linux host before any public learner traffic.

## Deliberately deferred

- [ ] Production connection to the existing CodeLeaf Render backend (reserved until all six phases are complete).
- [ ] Removing JDoodle or changing production CodeLeaf provider routing.
- [ ] Nested directory trees, dependency manifests and project build systems; Piston's API accepts root-level filenames only.
- [ ] Isolated MySQL/PostgreSQL database service adapters and scientific Python environment images.
