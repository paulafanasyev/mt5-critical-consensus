$ErrorActionPreference = "Stop"
$root = "C:\Users\Administrator\Documents\Qoder\2026-10-30\c3d543b8\repo"
$log = Join-Path $root "agent_data\micro_watch.jsonl"

function Write-Event([string]$event, [hashtable]$fields) {
    $row = @{ ts = (Get-Date).ToUniversalTime().ToString("o"); event = $event } + $fields
    ($row | ConvertTo-Json -Compress) | Add-Content -Path $log -Encoding utf8
}

while ($true) {
    try {
        $terminal = Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" |
            Where-Object { $_.CommandLine -like "*GTC*" }
        if (-not $terminal) {
            Start-Process -FilePath "C:\Program Files\GTC Global SA MetaTrader 5\terminal64.exe"
            Write-Event "terminal_restarted" @{}
        }
    } catch {
        Write-Event "watch_error" @{ error = $_.Exception.Message }
    }
    Start-Sleep -Seconds 10
}

