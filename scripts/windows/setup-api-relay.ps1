# ONE-TIME setup for the IBKR LIVE API relay. Run AT THE PC in an ELEVATED PowerShell
# ("Run as administrator"):
#     powershell -ExecutionPolicy Bypass -File D:\quant\scripts\windows\setup-api-relay.ps1
#
# Registers scheduled task "\IBKR\API Relay" (your account, Run with highest privileges,
# at LOGON) that runs scripts/windows/win-api-relay.ps1 -- the user-mode TCP relay
# 0.0.0.0:4003 -> 127.0.0.1:4001 which satisfies the gateway's TrustedIPs=127.0.0.1 while
# letting WSL/Docker containers reach the live API. See win-api-relay.ps1 for the full
# chain diagram.
#
# Idempotent: re-running just re-registers the same task.
$ErrorActionPreference = 'Stop'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host "Please run this in an ELEVATED PowerShell (Run as administrator)." -ForegroundColor Red; exit 1 }

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"D:\quant\scripts\windows\win-api-relay.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 9999) -MultipleInstances IgnoreNew -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskPath '\IBKR\' -TaskName 'API Relay' -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description 'User-mode TCP relay 0.0.0.0:4003 -> 127.0.0.1:4001. Lets WSL/Docker reach the LIVE IBKR Gateway API despite TrustedIPs=127.0.0.1. Runs at logon; see scripts/windows/win-api-relay.ps1.' -Force | Out-Null
Write-Host "Registered task \IBKR\API Relay (runs as $user at logon, highest privileges)." -ForegroundColor Green

# Start it now so we don't wait for the next logon.
Start-ScheduledTask -TaskPath '\IBKR\' -TaskName 'API Relay'
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskPath '\IBKR\' -TaskName 'API Relay'
$info = $task | Get-ScheduledTaskInfo
Write-Host "Task state: $($task.State)  LastRun: $($info.LastRunTime)  LastResult: 0x$('{0:X8}' -f $info.LastTaskResult)" -ForegroundColor Cyan

# Verify the relay is listening.
$listening = Get-NetTCPConnection -LocalPort 4003 -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "Relay is LISTENING on 0.0.0.0:4003." -ForegroundColor Green
} else {
    Write-Host "Relay not listening yet -- check C:\IBC-Live\Logs\relay.log" -ForegroundColor Yellow
}
