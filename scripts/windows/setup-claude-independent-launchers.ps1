# Registers the on-demand Task Scheduler launchers that keep the LIVE gateway and the watchdog
# independent of the Claude desktop app (2026-11-26, RETRO_2FA_2026-11-24 §4b).
#
# Anything started from a Claude Code shell joins that session's Windows job and dies when the
# Claude app quits. watchdog.ps1 and ibkr-live-login.ps1 (via ibkr-common.ps1 Test-UnderClaude)
# detect that and hand over to these tasks, which run as the logged-on user in the interactive
# session (toasts and the passkey prompt still show), outside any Claude job:
#   \IBKR\Quant Watchdog Start  -> the Startup-folder watchdog launcher (QuantDashboardWatchdog.vbs)
#   \IBKR\Live Login            -> ibkr-live-login-task.vbs (login, unattended)
# ibkr-live-autostart.ps1 uses the existing \IBKR\Live Auto-Start task.
# No triggers: they only run when started. Idempotent: re-running replaces them.
$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
# The VBS launchers exit at once; the 5 min limit only bounds a stuck wscript.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$wscript = "$env:WINDIR\System32\wscript.exe"
$startupVbs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\QuantDashboardWatchdog.vbs'

$tasks = @(
    @{ Name = 'Quant Watchdog Start'; Arg = "`"$startupVbs`""
       Desc = 'On demand: starts D:\quant\watchdog.ps1 outside any Claude Code session (watchdog.ps1 hands over to this when started from Claude).' }
    @{ Name = 'Live Login'; Arg = '"D:\quant\scripts\windows\ibkr-live-login-task.vbs"'
       Desc = 'On demand: runs ibkr-live-login.ps1 -Unattended outside any Claude Code session, so IBC-Live does not die when the Claude app quits.' }
)
foreach ($t in $tasks) {
    Register-ScheduledTask -TaskPath '\IBKR\' -TaskName $t.Name -Action (New-ScheduledTaskAction -Execute $wscript -Argument $t.Arg) `
        -Settings $settings -Principal $principal -Description $t.Desc -Force | Out-Null
    $r = Get-ScheduledTask -TaskPath '\IBKR\' -TaskName $t.Name
    "registered \IBKR\$($t.Name): state=$($r.State) action=$($r.Actions[0].Execute) $($r.Actions[0].Arguments)"
}
