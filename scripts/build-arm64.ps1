[CmdletBinding()]
param(
    [string]$ImageTag = "codeleaf-piston:arm64-validation",
    [string]$PistonRef = "de2b365ac759670a3a0d13ea208a0869a92c7e64",
    [string]$IsolateRef = "af6db68042c3aa0ded80787fbb78bc0846ea2114",
    [switch]$NoCache,
    [switch]$SkipImageSmokeTest
)

$ErrorActionPreference = "Stop"
$context = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\piston-arm64"))
$dockerfile = Join-Path $context "Dockerfile"

if (-not (Test-Path $dockerfile)) { throw "Piston Dockerfile not found: $dockerfile" }

Write-Host "Building custom Piston image for linux/arm64."
Write-Host "Piston source commit: $PistonRef"
Write-Host "Isolate source commit: $IsolateRef"
Write-Host "Image tag: $ImageTag"

$dockerArgs = @(
    "buildx", "build",
    "--platform", "linux/arm64",
    "--pull",
    "--load",
    "--build-arg", "PISTON_REF=$PistonRef",
    "--build-arg", "ISOLATE_REF=$IsolateRef",
    "--tag", $ImageTag,
    "--file", $dockerfile
)
if ($NoCache) { $dockerArgs += "--no-cache" }
$dockerArgs += $context

& docker @dockerArgs
if ($LASTEXITCODE -ne 0) {
    throw "ARM64 Piston image build failed. Do not replace it with the official AMD64 image."
}

$architecture = docker image inspect $ImageTag --format '{{.Os}}/{{.Architecture}}'
if ($LASTEXITCODE -ne 0 -or $architecture.Trim() -ne "linux/arm64") {
    throw "Image metadata check failed. Expected linux/arm64, got '$architecture'."
}
Write-Host "PASS: Docker image metadata is $($architecture.Trim())."

if (-not $SkipImageSmokeTest) {
    & (Join-Path $PSScriptRoot "validate-arm64-image.ps1") -ImageTag $ImageTag
    if ($LASTEXITCODE -ne 0) { throw "Piston ARM64 image smoke test failed." }
}
Write-Host "Build and requested validation stages completed."
