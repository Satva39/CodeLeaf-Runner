[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found."
    }
}

Assert-Command "docker"

Write-Host "== Docker daemon =="
docker info --format "OSType={{.OSType}} Architecture={{.Architecture}} CgroupVersion={{.CgroupVersion}}"
if ($LASTEXITCODE -ne 0) {
    throw "Docker Engine is not reachable. Start Docker Desktop and wait until it is ready."
}

$osType = docker info --format '{{.OSType}}'
if ($osType.Trim() -ne "linux") {
    throw "Docker Desktop must use Linux containers. Current OSType is '$($osType.Trim())'."
}

Write-Host "`n== Docker Buildx =="
docker buildx version
if ($LASTEXITCODE -ne 0) { throw "Docker Buildx is unavailable." }

$builder = docker buildx inspect --bootstrap 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { throw "Buildx builder could not bootstrap." }
if ($builder -notmatch "linux/arm64") {
    throw "The selected Buildx builder does not advertise linux/arm64. Enable ARM64/QEMU emulation in Docker Desktop, then rerun this script."
}
Write-Host "PASS: Buildx advertises linux/arm64."

$cgroup = docker info --format '{{.CgroupVersion}}'
if ($cgroup.Trim() -eq "2") {
    Write-Host "PASS: Docker host reports cgroup v2."
} else {
    Write-Warning "Docker host reports cgroup version '$($cgroup.Trim())'. ARM64 image build can continue, but Piston/Isolate runtime validation needs a Linux cgroup-v2 host."
}

Write-Host "`nPrerequisite check completed. No cloud resources were created."
