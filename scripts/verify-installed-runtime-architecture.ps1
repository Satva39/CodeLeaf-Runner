[CmdletBinding()]
param(
    [string]$PistonContainer = "codeleaf-runner-piston-1"
)

$ErrorActionPreference = "Stop"
$running = docker inspect --format '{{.State.Running}}' $PistonContainer 2>$null
if ($LASTEXITCODE -ne 0 -or $running.Trim() -ne "true") {
    throw "Piston container '$PistonContainer' is not running. Start the ARM64 Compose stack first."
}

Write-Host "Scanning installed Piston package files for ELF architecture..."
$lines = docker exec $PistonContainer sh -lc "find /piston/packages -type f -exec file '{}' \; 2>/dev/null"
if ($LASTEXITCODE -ne 0) { throw "Could not scan /piston/packages inside Piston." }

$elf = @($lines | Where-Object { $_ -match "ELF" })
if ($elf.Count -eq 0) {
    Write-Warning "No ELF files were detected under /piston/packages. Runtime architecture remains UNVERIFIED; run package provisioning and inspect package layout."
    exit 2
}

$bad = @($elf | Where-Object { $_ -notmatch "(?i)(ARM aarch64|AArch64)" })
foreach ($line in $elf) {
    if ($line -match "(?i)(ARM aarch64|AArch64)") {
        Write-Host "ARM64: $line"
    } else {
        Write-Host "NON-ARM64: $line"
    }
}
if ($bad.Count -gt 0) {
    throw "Found $($bad.Count) ELF file(s) that are not identified as AArch64. Do not mark those runtimes compatible."
}
Write-Host "PASS: all detected ELF files in installed Piston package files report AArch64."
Write-Host "Run the existing CodeLeaf Runner runtime smoke tests as a separate execution gate."
