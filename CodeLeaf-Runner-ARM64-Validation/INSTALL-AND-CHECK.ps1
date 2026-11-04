[CmdletBinding()]
param(
    [string]$ImageTag = "codeleaf-piston:arm64-validation"
)

$ErrorActionPreference = "Stop"
$scriptDir = Join-Path $PSScriptRoot "scripts"

Write-Host "STEP 1/3: Check Docker and Buildx prerequisites"
& (Join-Path $scriptDir "check-arm64-build-prerequisites.ps1")
if ($LASTEXITCODE -ne 0) { throw "Prerequisite checks failed." }

Write-Host "`nSTEP 2/3: Validate ARM64 manifests for all external service/base images"
& (Join-Path $scriptDir "validate-base-image-manifests.ps1")
if ($LASTEXITCODE -ne 0) { throw "Base image manifest checks failed." }

Write-Host "`nSTEP 3/3: Build and smoke-test the custom ARM64 Piston image"
& (Join-Path $scriptDir "build-arm64.ps1") -ImageTag $ImageTag
if ($LASTEXITCODE -ne 0) { throw "Piston ARM64 build/validation failed." }

Write-Host "`nSUCCESS: local ARM64 image validation package completed."
Write-Host "Next: follow docs\ARM64_VALIDATION.md to run the complete Runner stack and runtime tests."
