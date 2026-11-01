# Start the demo system. Keeps a running terminal untouched: restarting it with /config
# wipes the WebRequest allowlist, so the terminal is only launched when it is absent.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$terminalDir = "C:\Program Files\MetaTrader 5"
$policy = Join-Path $root "agent_data\live_gold_ema_policy.yaml"
$db = Join-Path $root "agent_data\demo_gold.db"
$token = "dsh-demo-20261101-Xk7Qp2Rt9Lm4Vb8Nw3Zc6Hd1"

$startedTerminal = $false
if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath "$terminalDir\terminal64.exe" -ArgumentList "/config:C:\mt5-start-demo.ini"
    $startedTerminal = $true
    Start-Sleep -Seconds 14
    Write-Host "Terminal started. If the EA logs 4014, tick 'Allow WebRequest for listed URL'"
    Write-Host "and add http://127.0.0.1:8000 (Ctrl+O -> Expert Advisors)."
} else {
    Write-Host "Terminal already running; not restarting it (the allowlist would be wiped)."
}

$runner = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*run_mt5_demo_trading*" })
if ($runner.Count -eq 0) {
    $env:MT5_BRIDGE_TOKEN = $token
    Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList @(
        "$root\scripts\run_mt5_demo_trading.py", "--policy", $policy, "--db", $db,
        "--symbols", "XAUUSD", "--cycles", "0", "--interval-seconds", "30",
        "--execution", "auto"
    ) -RedirectStandardOutput "$root\agent_data\demo_gold.out" -RedirectStandardError "$root\agent_data\demo_gold.err" -WindowStyle Hidden
    Start-Sleep -Seconds 12
    Write-Host "Runner started."
} else {
    Write-Host "Runner already running ($($runner.Count) process(es))."
}

& "$root\scripts\system_status.ps1"
