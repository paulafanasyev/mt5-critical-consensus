# Stop the demo system. By default only the runner is stopped; the terminal is left running
# so its WebRequest allowlist survives. Pass -All to close the terminal too.
param([switch]$All)
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)

$runner = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*run_mt5_demo_trading*" })
foreach ($process in $runner) {
    Stop-Process -Id $process.ProcessId -Force
    Write-Host "Stopped runner pid $($process.ProcessId)"
}
if ($runner.Count -eq 0) { Write-Host "Runner was not running." }
Start-Sleep -Seconds 2

if ($All) {
    $terminal = Get-Process terminal64 -ErrorAction SilentlyContinue
    if ($terminal) {
        $terminal.CloseMainWindow() | Out-Null
        Start-Sleep -Seconds 5
        if (Get-Process terminal64 -ErrorAction SilentlyContinue) {
            Stop-Process -Name terminal64 -Force
            Write-Host "Terminal force-stopped (the WebRequest allowlist may be lost)."
        } else {
            Write-Host "Terminal closed cleanly."
        }
    }
} else {
    Write-Host "Terminal left running; use -All to close it."
}

& "$root\scripts\system_status.ps1"
