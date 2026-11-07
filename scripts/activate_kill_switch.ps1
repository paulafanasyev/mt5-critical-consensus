param(
    [Parameter(Mandatory = $false)]
    [string] $BaseUrl = "http://127.0.0.1:8000",
    [Parameter(Mandatory = $false)]
    [string] $Reason = "Operator emergency stop"
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($env:MT5_BRIDGE_TOKEN) -or $env:MT5_BRIDGE_TOKEN.Length -lt 24) {
    throw "MT5_BRIDGE_TOKEN is missing or too short; emergency-stop request was not sent."
}

$body = @{
    active = $true
    actor_id = if ([string]::IsNullOrWhiteSpace($env:USERNAME)) { "operator" } else { $env:USERNAME }
    reason = $Reason
} | ConvertTo-Json -Depth 4 -Compress

$headers = @{ "X-Bridge-Key" = $env:MT5_BRIDGE_TOKEN }
$uri = $BaseUrl.TrimEnd("/") + "/v1/risk/kill-switch"
$response = Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -ContentType "application/json" -Body $body -TimeoutSec 5

if ($response.kill_switch_active -ne $true -or $response.status -ne "PERSISTED") {
    throw "Emergency-stop activation was not confirmed by the server; verify the persisted state immediately."
}
if ($response.event_hash -notmatch "^[0-9a-f]{64}$" -or -not $response.updated_at) {
    throw "Emergency-stop answer carried no durable event hash or timestamp; treating it as unconfirmed."
}

# The read the advisor performs, from this operator's own seat: a stop the polled state does not
# agree with is not a stop, so this checks the record the advisor reads instead of the answer alone.
$polled = Invoke-RestMethod -Method Get -Uri $uri -Headers $headers -TimeoutSec 5
if ($polled.kill_switch_active -ne $true) {
    throw "The server accepted the stop but its own kill-switch read does not report it armed."
}

Write-Output ("Kill switch ACTIVE. State persisted at {0}. Event hash: {1}" -f $response.updated_at, $response.event_hash)
Write-Output ("Advisor reads reason: {0}" -f $polled.kill_switch_reason)
Write-Output "This blocks new command polling; it does not close open positions or cancel broker-side orders."
