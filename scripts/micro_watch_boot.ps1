$ErrorActionPreference = "SilentlyContinue"
$root = "C:\Users\Administrator\Documents\Qoder\2026-10-30\c3d543b8\repo"
$watch = Join-Path $root "scripts\micro_watch.ps1"

$alive = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like "*micro_watch.ps1*" }
if (-not $alive) {
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", $watch) `
        -WindowStyle Hidden
}
