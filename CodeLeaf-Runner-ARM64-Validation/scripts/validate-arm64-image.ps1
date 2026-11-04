[CmdletBinding()]
param(
    [string]$ImageTag = "codeleaf-piston:arm64-validation",
    [int]$HostPort = 22000,
    [int]$WaitSeconds = 150
)

$ErrorActionPreference = "Stop"
$container = "codeleaf-piston-arm64-validation"
$volume = "codeleaf-piston-arm64-validation-data"

function Invoke-DockerChecked {
    param([Parameter(Mandatory = $true)][string[]]$DockerArgs)

    $output = & docker @DockerArgs
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "docker $($DockerArgs -join ' ') failed with exit code $exitCode"
    }
    return $output
}

function Remove-TestResources {
    param([switch]$Strict)

    # Query resource lists first. Never call `docker rm` or `docker volume rm`
    # for a resource that is absent: with PowerShell 7 and ErrorActionPreference
    # set to Stop, Docker's expected "not found" stderr can terminate the script.
    try {
        $containerNames = @(& docker ps -a --format "{{.Names}}")
        $listExitCode = $LASTEXITCODE
        if ($listExitCode -ne 0) {
            throw "Could not list Docker containers (exit $listExitCode)."
        }
        if ($containerNames -contains $container) {
            $null = & docker rm -f $container
            $removeExitCode = $LASTEXITCODE
            if ($removeExitCode -ne 0) {
                throw "Could not remove test container '$container' (exit $removeExitCode)."
            }
        }
    }
    catch {
        if ($Strict) { throw }
        Write-Warning "Test-container cleanup issue: $($_.Exception.Message)"
    }

    try {
        $volumeNames = @(& docker volume ls --format "{{.Name}}")
        $listExitCode = $LASTEXITCODE
        if ($listExitCode -ne 0) {
            throw "Could not list Docker volumes (exit $listExitCode)."
        }
        if ($volumeNames -contains $volume) {
            $null = & docker volume rm $volume
            $removeExitCode = $LASTEXITCODE
            if ($removeExitCode -ne 0) {
                throw "Could not remove test volume '$volume' (exit $removeExitCode)."
            }
        }
    }
    catch {
        if ($Strict) { throw }
        Write-Warning "Test-volume cleanup issue: $($_.Exception.Message)"
    }
}

try {
    $arch = & docker image inspect $ImageTag --format '{{.Os}}/{{.Architecture}}'
    $inspectExitCode = $LASTEXITCODE
    if ($inspectExitCode -ne 0 -or (($arch -join '').Trim() -ne "linux/arm64")) {
        throw "Image is not linux/arm64. Found '$arch' (inspect exit $inspectExitCode)."
    }

    # Clear only resources owned by this validation script, and only if present.
    Remove-TestResources -Strict

    $null = Invoke-DockerChecked -DockerArgs @("volume", "create", $volume)

    $null = Invoke-DockerChecked -DockerArgs @(
        "run", "-d", "--name", $container, "--platform", "linux/arm64", "--privileged",
        "--publish", "127.0.0.1:${HostPort}:2000",
        "--volume", "${volume}:/piston",
        "--tmpfs", "/tmp:exec",
        "--env", "PISTON_DISABLE_NETWORKING=true",
        "--env", "PISTON_MAX_PROCESS_COUNT=64",
        "--env", "PISTON_MAX_OPEN_FILES=1024",
        $ImageTag
    )

    $baseUrl = "http://127.0.0.1:$HostPort"
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    $ready = $false
    $lastHttpError = "No response yet."
    while ((Get-Date) -lt $deadline) {
        try {
            $rootResponse = Invoke-RestMethod "$baseUrl/" -TimeoutSec 4
            if ($rootResponse.message -match '^Piston v') {
                $ready = $true
                break
            }
            $lastHttpError = "Unexpected root response: $($rootResponse | ConvertTo-Json -Compress -Depth 5)"
        }
        catch {
            $lastHttpError = $_.Exception.Message
            Start-Sleep -Seconds 3
        }
    }
    if (-not $ready) {
        Write-Host "--- Piston container logs ---"
        try { & docker logs $container } catch { Write-Warning $_ }
        throw "Piston did not become ready within $WaitSeconds seconds. Last HTTP result: $lastHttpError. On an x86-64 Windows host, privileged Isolate/cgroup behavior may be limited by QEMU/WSL2; native ARM64 Linux validation remains required."
    }

    $nodeArch = & docker exec $container node -p "process.arch"
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0 -or (($nodeArch -join '').Trim() -ne "arm64")) {
        throw "Node reports '$nodeArch' (exit $exitCode); expected arm64."
    }

    $machineArch = & docker exec $container uname -m
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0 -or (($machineArch -join '').Trim() -notin @("aarch64", "arm64"))) {
        throw "Container reports uname -m '$machineArch' (exit $exitCode); expected aarch64."
    }

    $isolateFile = & docker exec $container sh -lc "readelf -h /usr/local/bin/isolate | grep Machine"
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0 -or (($isolateFile -join ' ') -notmatch "AArch64")) {
        throw "Isolate executable does not report AArch64: $isolateFile (exit $exitCode)"
    }

    $packages = Invoke-RestMethod "$baseUrl/api/v2/packages" -TimeoutSec 20
    $runtimes = Invoke-RestMethod "$baseUrl/api/v2/runtimes" -TimeoutSec 20
    if ($null -eq $packages -or $null -eq $runtimes) {
        throw "Piston package/runtime catalogue endpoint returned null."
    }

    Write-Host "PASS: OCI image metadata = linux/arm64"
    Write-Host "PASS: Node process.arch = $($nodeArch.Trim())"
    Write-Host "PASS: container uname -m = $($machineArch.Trim())"
    Write-Host "PASS: Isolate ELF machine = AArch64"
    Write-Host "PASS: Piston root, package catalogue and runtime catalogue endpoints respond"
    Write-Host "Piston package catalogue entries visible: $(@($packages).Count)"
    Write-Host "Installed runtime entries in clean test volume: $(@($runtimes).Count)"
    Write-Host "NOTE: zero installed runtimes is expected in a clean image."
    Write-Host "NOTE: this validates the ARM64 image under local Docker emulation, not native OCI hardware or full sandbox execution."
}
catch {
    $failure = $_
    try {
        $containerNames = @(& docker ps -a --format "{{.Names}}")
        if ($LASTEXITCODE -eq 0 -and ($containerNames -contains $container)) {
            Write-Host "--- Piston container logs after validation failure ---"
            & docker logs $container
        }
    }
    catch {
        Write-Warning "Could not collect Piston logs: $($_.Exception.Message)"
    }
    throw $failure
}
finally {
    # Cleanup must never replace the actual build/API failure with a secondary
    # "no such volume/container" error.
    Remove-TestResources
}
