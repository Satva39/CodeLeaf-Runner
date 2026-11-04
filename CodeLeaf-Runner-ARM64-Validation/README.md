# CodeLeaf Runner ARM64 Validation Package — v4

This additive package builds a custom ARM64 Piston image from pinned upstream
source and validates its container startup, architecture markers, and API.

**v4 fixes the PowerShell/Docker cleanup failure from v3.** The v3 build had
already produced an image with `linux/arm64` metadata; its validator stopped
when it tried to remove a temporary volume before checking whether that volume
existed. v4 makes cleanup idempotent and preserves the original smoke-test
failure, if any.

No Oracle server is created or rented. See `PATCH-INSTRUCTIONS.txt` and
`docs/ARM64_VALIDATION.md` before proceeding to full-stack and native OCI tests.
