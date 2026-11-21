# The ONE planned YubiKey touch per week (IBKR_WEEKLY_2FA_SPEC §3.1, 2026-11-21). All HKT.
#
# IBKR forces a full passkey re-authentication once a week around its Sunday reset
# ("Re-login is required", seen Sun 16:54-18:40 HKT). Instead of letting that land wherever
# IBKR's kick or IBC's (removed) ColdRestartTime put it, we schedule it:
#
#   -Phase reminder  T-10min: toast + one Telegram page "weekly touch at <T> HKT"
#   -Phase touch     T (default Sun 20:35 HKT, C:\IBC-Live\weekly-touch.json):
#                      week already satisfied (a full auth since Sun 14:00 HKT) -> skip
#                      gateway logged in (token survived the boundary)  -> conditional cold:
#                        stop IBC now and log in, so IBKR cannot force it mid-week instead
#                      otherwise                                         -> launch the login
#   -Phase report    Mon 09:00 HKT: last 7 days from the touch ledger (planned/unplanned,
#                    warm-restart verdicts). Any unplanned touch -> critical; else digest.
#
# Dispatched by watchdog.ps1 (Get-DueWeeklyPhase, catch-up if the box was off at the minute).
# Idempotent per week via C:\IBC-Live\Logs\weekly-touch.state.json. Log: weekly-touch.log.
# -DryRun: decide and log, change nothing (no push, no login, no state write).
param(
    [Parameter(Mandatory)][ValidateSet('reminder', 'touch', 'report')][string]$Phase,
    [switch]$DryRun
)
. 'D:\quant\scripts\windows\ibkr-common.ps1'
. 'D:\quant\scripts\windows\ib-api-handshake.ps1'
$LogFile = 'C:\IBC-Live\Logs\weekly-touch.log'
if ($DryRun) { $global:IbkrNoPush = $true }
function Log($m) { Write-IbkrLog $LogFile ("[{0}{1}] {2}" -f $Phase, $(if ($DryRun) { ' dry-run' } else { '' }), $m) }

$now = Get-HktNow
$w = Get-WeeklyWindow $now
$st = Read-WeeklyState
Log ("start: now={0} touch={1} boundary={2} week={3}" -f (Format-Hkt $now), (Format-Hkt $w.Touch), (Format-Hkt $w.GoverningBoundary), $w.WeekKey)

switch ($Phase) {
    'reminder' {
        if ($st.reminderWeek -eq $w.WeekKey) { Log 'already sent this week'; exit 0 }
        $msg = "Weekly IBKR YubiKey touch at {0} (in {1} min). Key in a port directly on the PC, not the hub." -f (Format-Hkt $w.Touch), [int]($w.Touch - $now).TotalMinutes
        Log "reminder: $msg"
        if (-not $DryRun) {
            $st.reminderWeek = $w.WeekKey; Save-WeeklyState $st
            Show-IbkrToast 'IBKR weekly touch soon' $msg
            Send-GatewayAlert 'critical' 'weekly-touch:reminder' $msg
        }
        exit 0
    }
    'touch' {
        if ($st.touchWeek -eq $w.WeekKey) { Log 'already handled this week'; exit 0 }
        try { $null = Update-TouchLedger } catch { }
        $logged = Test-IbApiHandshake -HostName '127.0.0.1' -Port 4001 -TimeoutSec 6
        $sat = Test-WeekSatisfied $now
        if ($sat -and $logged) { $action = 'skip' }
        elseif ($logged) { $action = 'cold' }
        else { $action = 'login' }
        Log ("decision={0} (loggedIn={1}, weekSatisfied={2})" -f $action, $logged, $sat)
        if ($DryRun) { exit 0 }
        $st.touchWeek = $w.WeekKey
        if ($action -eq 'skip') {
            Save-WeeklyState $st
            Send-GatewayAlert 'error' 'weekly-touch:skipped' 'weekly touch not needed: already re-authenticated since the Sunday boundary'
            exit 0
        }
        $st.touchLaunchedAt = (Get-HktNow).ToString('yyyy-MM-dd HH:mm:ss'); Save-WeeklyState $st
        if ($action -eq 'cold') {
            # IBKR did not force the re-auth yet; do it NOW, while the human expects it,
            # rather than at an unpredictable mid-week restart (spec D2).
            Log 'conditional cold: stopping IBC-Live for the planned weekly login'
            Archive-IbcLogs 'weeklycold' | Out-Null
            Stop-IbcLive
            Start-Sleep -Seconds 5
        }
        Show-IbkrToast 'IBKR weekly touch NOW' 'The passkey prompt is starting -- touch the YubiKey when the Security dialog shows.'
        Send-GatewayAlert 'critical' 'weekly-touch:now' ("Weekly IBKR login starting now ({0}) -- touch the YubiKey" -f (Format-Hkt (Get-HktNow)))
        Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', 'D:\quant\scripts\windows\ibkr-live-login.ps1',
            '-Unattended', '-Reason', 'weekly-touch', '-TimeoutSec', '600') -WorkingDirectory 'D:\quant' | Out-Null
        Log 'login launched (unattended, 10 min window; the watchdog re-prompts after that)'
        exit 0
    }
    'report' {
        $rk = $w.Report.ToString('yyyy-MM-dd')
        if ($st.reportWeek -eq $rk) { Log 'already reported'; exit 0 }
        try { $null = Update-TouchLedger } catch { }
        $from = $now.AddDays(-7)
        $rows = @(Read-TouchLedger | Where-Object { [datetime]::ParseExact($_.hkt.Substring(0, 19), 'yyyy-MM-dd HH:mm:ss', $null) -ge $from })
        $full = @($rows | Where-Object { $_.kind -eq 'full-auth' })
        $unpl = @($full | Where-Object { -not $_.planned })
        $warm = @($rows | Where-Object { $_.kind -eq 'warm-restart' })
        $rej = @($warm | Where-Object { $_.verdict -ne 'accepted' })
        $detail = ($unpl | ForEach-Object { "$($_.hkt) $($_.cause)" }) -join '; '
        $msg = "IBKR touches, 7d to {0}: {1} full logins ({2} planned, {3} unplanned{4}); warm restarts {5} ok / {6} failed" -f `
            (Format-Hkt $now), $full.Count, ($full.Count - $unpl.Count), $unpl.Count, $(if ($detail) { ": $detail" } else { '' }), ($warm.Count - $rej.Count), $rej.Count
        Log $msg
        if ($DryRun) { exit 0 }
        $st.reportWeek = $rk; Save-WeeklyState $st
        if ($unpl.Count -gt 0 -or $full.Count -eq 0) { Send-GatewayAlert 'critical' 'weekly-touch:report' $msg }
        else { Send-GatewayAlert 'error' 'weekly-touch:report' $msg }
        exit 0
    }
}
