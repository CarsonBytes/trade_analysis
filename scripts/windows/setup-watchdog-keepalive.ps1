# Registers task \IBKR\Quant Watchdog Keepalive (2026-11-21): runs ensure-quant-watchdog.vbs at
# logon and every 5 minutes, as the logged-on user (same session as the Startup-folder launch, so
# the watchdog's toasts still show). Idempotent: re-running replaces the task.
# Repetition is a UTC instant + interval, so a Windows timezone change does not move it.
$action   = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wscript.exe" -Argument '"D:\quant\scripts\windows\ensure-quant-watchdog.vbs"'
$every5   = New-ScheduledTaskTrigger -Once -At ([DateTime]::UtcNow.AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 5)
$atLogon  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskPath '\IBKR\' -TaskName 'Quant Watchdog Keepalive' -Action $action `
    -Trigger @($every5, $atLogon) -Settings $settings -Principal $principal -Force `
    -Description 'Restarts D:\quant\watchdog.ps1 if it is gone or its heartbeat is stale (scripts\windows\ensure-quant-watchdog.ps1).' | Out-Null
$t = Get-ScheduledTask -TaskPath '\IBKR\' -TaskName 'Quant Watchdog Keepalive'
"registered: state=$($t.State); triggers: " + (($t.Triggers | ForEach-Object { "$($_.CimClass.CimClassName -replace 'MSFT_Task','') start=$($_.StartBoundary) every=$($_.Repetition.Interval) for=$($_.Repetition.Duration)" }) -join ' | ')
