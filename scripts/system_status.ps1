# Status of the demo system: terminal, bridge, runner, EA log, account.
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$terminalDir = "C:\Program Files\MetaTrader 5"

$terminal = Get-Process terminal64 -ErrorAction SilentlyContinue
"terminal      : " + $(if ($terminal) { "running (pid $($terminal.Id), since $($terminal.StartTime.ToString('HH:mm')))" } else { "STOPPED" })
$listen = @(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
"bridge :8000  : " + $(if ($listen) { "listening (pid $((($listen.OwningProcess | Sort-Object -Unique) -join ',')))" } else { "DOWN" })
$runner = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*run_mt5_demo_trading*" })
# `.venv\Scripts\python.exe` is a stub that re-spawns the identical command line in the base
# interpreter, so one run is two processes here. A raw count calls that "2 process(es)" — which
# reads like a double launch against one account — and would call a genuine double launch 4.
# Dropping the child of another runner keeps one entry per decision loop.
$stubOfRunner = @($runner | ForEach-Object { $_.ParentProcessId })
$loops = @($runner | Where-Object { $stubOfRunner -notcontains $_.ProcessId })
"runner        : " + $(if ($loops.Count -gt 0) { "$($loops.Count) loop (pid $((($loops.ProcessId | Sort-Object -Unique) -join ',')))" } else { "STOPPED" })

$log = Get-ChildItem "$terminalDir\MQL5\logs\*.log" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($log) {
    # The date must come from the directory, not from a literal: on 2026-11-02 this script read
    # 20261101.log, so it printed a clean tail while today's polls were all error=4014.
    "EA log (" + $log.Name + ") :"
    & "$root\.venv\Scripts\python.exe" "$root\scripts\check_ea_transport.py" `
        --log-dir "$terminalDir\MQL5\logs"
    if ($LASTEXITCODE -eq 1) { "  -> a command queued now expires at the bridge without reaching the broker." }
    if ($LASTEXITCODE -eq 2) { "  -> the check could not decide: read the meaning line above, and do not treat this as a dead transport." }
}

& "$root\.venv\Scripts\python.exe" -c @"
import MetaTrader5 as mt5
if not mt5.initialize():
    print('account       : MT5 initialize failed'); raise SystemExit
a = mt5.account_info()
print(f'account       : {a.login} {a.server} balance {a.balance:.2f} equity {a.equity:.2f} {a.currency}')
print(f'positions     : {mt5.positions_total()}')
mt5.shutdown()
"@ 2>$null
