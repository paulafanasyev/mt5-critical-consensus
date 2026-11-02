# Status of the demo system: terminal, bridge, runner, EA log, account.
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$terminalDir = "C:\Program Files\MetaTrader 5"

$terminal = Get-Process terminal64 -ErrorAction SilentlyContinue
"terminal      : " + $(if ($terminal) { "running (pid $($terminal.Id), since $($terminal.StartTime.ToString('HH:mm')))" } else { "STOPPED" })
$bridge = netstat -ano | Select-String ":8000.*LISTENING"
"bridge :8000  : " + $(if ($bridge) { "listening" } else { "DOWN" })
$runner = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*run_mt5_demo_trading*" })
"runner        : " + $(if ($runner.Count -gt 0) { "$($runner.Count) process(es)" } else { "STOPPED" })

$log = "$terminalDir\MQL5\logs\20261101.log"
if (Test-Path $log) {
    "EA log tail   :"
    Get-Content $log -Encoding Unicode | Select-Object -Last 3 | ForEach-Object { "  " + $_ }
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
