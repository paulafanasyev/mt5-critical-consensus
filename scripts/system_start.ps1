# Start the demo system. Keeps a running terminal untouched: restarting it with /config
# wipes the WebRequest allowlist, so the terminal is only launched when it is absent.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$terminalDir = "C:\Program Files\MetaTrader 5"
$policy = Join-Path $root "agent_data\live_gold_ema_policy.yaml"
$db = Join-Path $root "agent_data\demo_live_20261101.db"

# The bridge secret is never stored in a tracked file: this script is pushed, and a literal
# here would hand every reader the credential that authorizes order commands. It comes from
# the environment, or from the git-ignored file beside the kill-switch state, or it is
# created there on first use.
$tokenFile = Join-Path $root "agent_data\demo_bridge\bridge_token.txt"
if (-not $env:MT5_BRIDGE_TOKEN) {
    if (-not (Test-Path $tokenFile)) {
        $alpha = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_~."
        $bytes = New-Object byte[] 64
        (New-Object System.Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
        $fresh = -join (0..43 | ForEach-Object { $alpha[$bytes[$_] % $alpha.Length] })
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $tokenFile) | Out-Null
        [System.IO.File]::WriteAllText($tokenFile, $fresh, [System.Text.UTF8Encoding]::new($false))
    }
    $env:MT5_BRIDGE_TOKEN = (Get-Content $tokenFile -Raw).Trim()
}
if ($env:MT5_BRIDGE_TOKEN.Length -lt 24) { throw "bridge token is shorter than the EA accepts" }
$adminFile = Join-Path $root "agent_data\demo_bridge\admin_token.txt"
if (-not $env:MT5_KILL_SWITCH_ADMIN_TOKEN -and (Test-Path $adminFile)) {
    $env:MT5_KILL_SWITCH_ADMIN_TOKEN = (Get-Content $adminFile -Raw).Trim()
}

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
    Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList @(
        "$root\scripts\run_mt5_demo_trading.py", "--policy", $policy, "--db", $db,
        "--symbols", "XAUUSD,XAGUSD,EURUSD,GBPUSD", "--cycles", "0", "--interval-seconds", "20",
        "--execution", "auto"
    ) -RedirectStandardOutput "$root\agent_data\demo_gold.out" -RedirectStandardError "$root\agent_data\demo_gold.err" -WindowStyle Hidden
    Start-Sleep -Seconds 12
    Write-Host "Runner started."
} else {
    Write-Host "Runner already running ($($runner.Count) process(es))."
}

& "$root\scripts\system_status.ps1"
