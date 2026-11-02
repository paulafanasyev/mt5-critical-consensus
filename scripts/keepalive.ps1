# Keep the trading stack alive without anyone watching: if a piece is missing, start it.
# Registered as a Windows scheduled task, it runs every five minutes around the clock.
$ErrorActionPreference = "SilentlyContinue"
$root = "C:\Users\Administrator\Documents\Qoder\2026-10-30\c3d543b8\repo"
$python = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "agent_data\keepalive.log"
$time = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss")

function Write-Log([string]$message) {
    Add-Content -Path $log -Value "[$time] $message" -Encoding utf8
}

$terminal = Get-Process terminal64 -ErrorAction SilentlyContinue
if (-not $terminal) {
    Start-Process -FilePath "C:\Program Files\MetaTrader 5\terminal64.exe" -ArgumentList "/config:C:\mt5-start-demo.ini"
    Write-Log "terminal was down, started (WebRequest may need re-ticking)"
}

$swarm = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*agent_swarm*" }
if (-not $swarm) {
    Start-Process -FilePath $python -ArgumentList @(
        (Join-Path $root "scripts\agent_swarm.py"), "--interval-sec", "20"
    ) -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $root "agent_data\swarm.stdout.log") `
        -RedirectStandardError (Join-Path $root "agent_data\swarm.stderr.log")
    Write-Log "swarm was down, started"
}

$supervisor = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*swarm_supervisor*" }
if (-not $supervisor) {
    Start-Process -FilePath $python -ArgumentList @(
        (Join-Path $root "scripts\swarm_supervisor.py"), "--interval-sec", "60"
    ) -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $root "agent_data\supervisor.stdout.log") `
        -RedirectStandardError (Join-Path $root "agent_data\supervisor.stderr.log")
    Write-Log "supervisor was down, started"
}

Write-Log ("alive: terminal=" + [bool]$terminal + " swarm=" + [bool]$swarm + " supervisor=" + [bool]$supervisor)
