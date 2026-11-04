[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$requiredImages = @(
    "node:24-bookworm-slim",
    "buildpack-deps:bookworm",
    "python:3.12-slim",
    "redis:7.4-alpine",
    "mysql:8.4.13",
    "postgres:17.11-bookworm",
    "alpine/socat:latest"
)

$failed = @()
foreach ($image in $requiredImages) {
    Write-Host "`nChecking $image ..."
    $output = docker buildx imagetools inspect $image 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAIL: manifest inspection failed."
        Write-Host $output
        $failed += $image
        continue
    }

    if ($output -match '(?im)^\s*Platform:\s+linux/arm64(?:/v8)?\s*$') {
        Write-Host "PASS: linux/arm64 manifest found."
    } else {
        Write-Host "FAIL: no linux/arm64 manifest was found."
        $failed += $image
    }
}

Write-Host "`nChecking the official Piston image baseline ..."
$piston = docker buildx imagetools inspect ghcr.io/engineer-man/piston:latest 2>&1 | Out-String
if ($LASTEXITCODE -eq 0 -and $piston -match '(?im)^\s*Platform:\s+linux/arm64(?:/v8)?\s*$') {
    Write-Host "INFO: the official Piston tag now advertises ARM64; still build and validate the pinned custom image."
} else {
    Write-Host "EXPECTED BASELINE: official Piston tag does not advertise a usable ARM64 manifest; this package builds Piston from source."
}

if ($failed.Count -gt 0) {
    throw ("ARM64 manifests missing for: " + ($failed -join ", "))
}
Write-Host "`nPASS: all external base/service images required by this package publish ARM64 manifests."
