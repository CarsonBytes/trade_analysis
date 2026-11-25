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
#                    YubiKey absent (F2, RETRO_2FA_2026-11-24): never launch or cold-stop;
#                      nokey-wait = page once, the watchdog launches the login on insert;
#                      nokey-keep = session still logged in, keep it
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
        if ($st.reminderWeek -eq $w.WeekKey -and -not $DryRun) { Log 'already sent this week'; exit 0 }
        # F2 (RETRO_2FA_2026-11-24): Nov 22 the reminder said "key in a port" without checking.
        $hasKey = Test-YubiKeyPresent
        if ($hasKey) {
            $msg = "Weekly IBKR YubiKey touch at {0} (in {1} min). Key in a port directly on the PC, not the hub." -f (Format-Hkt $w.Touch), [int]($w.Touch - $now).TotalMinutes
            $title = 'IBKR weekly touch soon'
        } else {
            $msg = "Weekly IBKR YubiKey touch at {0} (in {1} min) -- the YubiKey is NOT PLUGGED IN. Plug it into a port directly on the PC now." -f (Format-Hkt $w.Touch), [int]($w.Touch - $now).TotalMinutes
            $title = 'IBKR weekly touch soon -- KEY NOT PLUGGED IN'
        }
        Log "reminder (keyPresent=$hasKey): $msg"
        if (-not $DryRun) {
            $st.reminderWeek = $w.WeekKey; Save-WeeklyState $st
            Show-IbkrToast $title $msg
            Send-GatewayAlert 'critical' 'weekly-touch:reminder' $msg
        }
        exit 0
    }
    'touch' {
        if ($st.touchWeek -eq $w.WeekKey -and -not $DryRun) { Log 'already handled this week'; exit 0 }
        if (-not $DryRun) { try { $null = Update-TouchLedger } catch { } }
        $logged = Test-IbApiHandshake -HostName '127.0.0.1' -Port 4001 -TimeoutSec 6
        $sat = Test-WeekSatisfied $now
        $hasKey = Test-YubiKeyPresent
        if ($sat -and $logged) { $action = 'skip' }
        elseif ($logged) { $action = $(if ($hasKey) { 'cold' } else { 'nokey-keep' }) }
        else { $action = $(if ($hasKey) { 'login' } else { 'nokey-wait' }) }
        Log ("decision={0} (loggedIn={1}, weekSatisfied={2}, keyPresent={3})" -f $action, $logged, $sat, $hasKey)
        if ($DryRun) { exit 0 }
        $st.touchWeek = $w.WeekKey
        if ($action -eq 'skip') {
            Save-WeeklyState $st
            Send-GatewayAlert 'error' 'weekly-touch:skipped' 'weekly touch not needed: already re-authenticated since the Sunday boundary'
            exit 0
        }
        if ($action -eq 'nokey-keep') {
            # Never stop a WORKING session when nobody can touch: keep it; IBKR may force the
            # re-auth later in the week and the watchdog handles that (F2).
            Save-WeeklyState $st
            Send-GatewayAlert 'critical' 'weekly-touch:nokey' 'Weekly IBKR touch skipped: the YubiKey is NOT plugged in. The current session keeps running; IBKR may force a re-login later this week -- leave the key plugged in.'
            exit 0
        }
        if ($action -eq 'nokey-wait') {
            # F2: do not launch a login that cannot work. Page once; the watchdog polls the key
            # and launches the login the moment it is inserted (touchLaunchedAt = this page, so
            # the watchdog does not page the same incident again).
            $st.touchLaunchedAt = (Get-HktNow).ToString('yyyy-MM-dd HH:mm:ss'); Save-WeeklyState $st
            $m = 'Weekly IBKR touch due NOW but the YubiKey is NOT plugged in -- plug it into a port directly on the PC; the login starts by itself when the key is detected'
            Show-IbkrToast 'IBKR weekly touch -- KEY NOT PLUGGED IN' $m
            Send-GatewayAlert 'critical' 'weekly-touch:nokey' $m
            Log 'no key: paged; the watchdog launches the login when the key appears'
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
