[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Symbol,

    [Parameter(Mandatory = $true)]
    [ValidateSet("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1")]
    [string]$Timeframe,

    [string]$TerminalPath,
    [string]$BootstrapFromUtc,
    [string]$PythonExe = "python",
    [string]$LogDirectory = ".\logs"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($env:DATABASE_URL)) {
    throw "DATABASE_URL is not set. Configure it in the process environment or secret manager."
}

$pythonCommand = Get-Command $PythonExe -ErrorAction SilentlyContinue
if ($null -eq $pythonCommand) {
    throw "Python executable not found: $PythonExe"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $resolvedLogDirectory = Join-Path $repoRoot $LogDirectory
    New-Item -ItemType Directory -Force -Path $resolvedLogDirectory | Out-Null
    $logName = "mt5-sync-{0}-{1}-{2}.log" -f $Symbol.ToUpperInvariant(), $Timeframe, (Get-Date -Format "yyyyMMdd-HHmmss")
    $logPath = Join-Path $resolvedLogDirectory $logName

    $terminalArgs = @()
    if (-not [string]::IsNullOrWhiteSpace($TerminalPath)) {
        if (-not (Test-Path -LiteralPath $TerminalPath -PathType Leaf)) {
            throw "MT5 terminal executable not found: $TerminalPath"
        }
        $terminalArgs = @("--terminal-path", $TerminalPath)
    }

    Write-Host "Starting read-only MT5 preflight. Log: $logPath"
    $checkArgs = @("scripts/check_mt5_terminal.py", "--symbol", $Symbol, "--timeframe", $Timeframe) + $terminalArgs
    & $pythonCommand.Source @checkArgs 2>&1 | Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) {
        throw "MT5 terminal preflight failed with exit code $LASTEXITCODE. Sync was not started."
    }

    $syncArgs = @("scripts/sync_mt5_market_data.py", "--symbol", $Symbol, "--timeframe", $Timeframe) + $terminalArgs
    if (-not [string]::IsNullOrWhiteSpace($BootstrapFromUtc)) {
        $syncArgs += @("--bootstrap-from-utc", $BootstrapFromUtc)
    }
    Write-Host "Preflight passed. Starting read-only market-data sync and due-forecast resolution."
    & $pythonCommand.Source @syncArgs 2>&1 | Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) {
        throw "MT5 synchronization failed with exit code $LASTEXITCODE. See $logPath"
    }

    Write-Host "Read-only MT5 sync completed. Log: $logPath"
}
finally {
    Pop-Location
}
