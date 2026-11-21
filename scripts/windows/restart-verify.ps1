# Daily self-check for the MIN-2FA restart chain (spec MIN-2FA, 2026-10-17).
# Task: \IBKR\Restart Verify, daily 20:50 -- 16 min after the 20:30 token restart, when
# restart-observe.ps1's 20:34 POST record is already in the log.
#
# Parses today's PRE/POST records from restart-observe.log, evaluates every invariant of a
# HEALTHY token restart, logs ONE LINE PER CHECK with the observed value (a future debug
# session never has to guess what was seen), then pushes ONE summary through
# gateway-push.sh (NOTIFICATION_SPEC v1):
#   PASS    -> `error`  : digest-eligible trail, does NOT page
#   FAIL    -> `critical`: pages Telegram (key `restart-verify`, 15min cooldown, 10/day cap)
#   SKIPPED -> `info`   : audit only -- never a page
#
# THREE-PART AUDIT TRAIL (by design):
#   1. C:\IBC-Live\Logs\restart-verify.log   START -> TASKINFO -> PRE/POST_RAW -> line-per-
#      CHECK -> OBSERVE -> SUMMARY -> PUSH_ISSUED -> PUSH_AUDIT (the alerts.jsonl outcome,
#      read back ~1s after the push so "was it delivered?" is answered HERE, not by guessing)
#   2. C:\IBC-Live\Logs\restart-observe.log  the PRE/POST records themselves (that script
#      logs START/END too, so a missed run is visible as an absent START)
#   3. D:\claude\alerts\audit\alerts.jsonl   gateway-push.sh's delivery record
#
# MANUAL RUNS:
#   .\restart-verify.ps1 -NoPush                            parse + full check log, no notify
#   .\restart-verify.ps1 -ForDate 2026-10-16 -NoPush        audit a past evening
#   .\restart-verify.ps1 -ObserveLog <path-to-copy>         test against synthetic records
#
# SKIPPED cases (deliberate, never page): catch-up run before 20:50 today; machine booted
# after 20:30 today; or BOTH records absent while the gateway is healthy right now (machine
# asleep/off across the window -- the token chain self-heals at the next healthy 20:30).
# If the gateway is NOT healthy, missing records are a real FAIL and page like they should.
param(
    [string]$ForDate   = (Get-Date -Format 'yyyy-MM-dd'),
    [string]$ObserveLog = 'C:\IBC-Live\Logs\restart-observe.log',
    [switch]$NoPush
)
$ErrorActionPreference = 'SilentlyContinue'
$VerifyLog = 'C:\IBC-Live\Logs\restart-verify.log'
$AuditFile = 'D:\claude\alerts\audit\alerts.jsonl'
$PushKey   = 'restart-verify'
$Distro    = 'Ubuntu'

$t0 = Get-Date
function Log($m) { "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m | Add-Content -Path $VerifyLog -Encoding UTF8 }

function Get-IbcPids {
    @(Get-CimInstance Win32_Process -Filter "Name='java.exe'" |
        Where-Object { $_.CommandLine -match 'IBC-Live' } |
        ForEach-Object { $_.ProcessId })
}
function Test-GatewayNow {
    $l = Get-NetTCPConnection -LocalPort 4001 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    [bool]($l -and (Get-IbcPids))
}
function Send-Push($lvl, $text) {
    # wsl.exe arg marshalling eats backslashes ("C:\IBC\..." arrived as
    # "C:IBC..."), mangling every path in Telegram/audit text -- force
    # forward slashes (fine for display) before the text crosses into WSL.
    $text = "$text".Replace('\', '/')
    if ($NoPush) { Log "PUSH_SKIPPED(-NoPush) level=$lvl key=$PushKey text='$text'"; return }
    & wsl.exe -d $Distro -- bash /home/cap/quant/scripts/gateway-push.sh "$lvl" "$PushKey" "$text" 2>$null | Out-Null
    Log "PUSH_ISSUED level=$lvl key=$PushKey exit=$LASTEXITCODE text='$text'"
    Start-Sleep -Seconds 1
    if (Test-Path $AuditFile) {
        $hit = Get-Content $AuditFile -Tail 20 |
            Where-Object { $_ -match ('"key":"' + $PushKey + '"') } |
            Select-Object -Last 1
        if ($hit) { Log "PUSH_AUDIT $hit" } else { Log "PUSH_AUDIT result=NOT_FOUND_in_last_20_audit_lines" }
    } else { Log "PUSH_AUDIT result=audit-file-missing path=$AuditFile" }
}

function Get-ExpectedCold($dateStr) {
    $d = [datetime]::ParseExact($dateStr, 'yyyy-MM-dd', $null)
    while ($d.DayOfWeek -ne 'Sunday') { $d = $d.AddDays(1) }
    if ($d.ToString('yyyy-MM-dd') -eq $dateStr) { $d = $d.AddDays(7) }
    return $d.ToString('yyyy/MM/dd') + ' 20:30'
}

Log "START date=$ForDate noPush=$([bool]$NoPush) ps=$PID script=$PSCommandPath"

# ---- task health first (always observed, even under SKIPPED) ---------------
$ti = Get-ScheduledTaskInfo -TaskPath '\IBKR\' -TaskName 'Restart Observe' -ErrorAction SilentlyContinue
$lastRunRaw = if ($ti) { $ti.LastRunTime } else { $null }
$ranToday = $lastRunRaw -and $lastRunRaw.Year -ge 2020 -and ($lastRunRaw.ToString('yyyy-MM-dd') -eq $ForDate)
$lastRun = if ($lastRunRaw -and $lastRunRaw.Year -ge 2020) { $lastRunRaw.ToString('yyyy-MM-dd HH:mm:ss') } else { 'never' }
$lastRes = if ($ti) { '0x{0:X8}' -f $ti.LastTaskResult } else { 'task-not-found' }
Log "TASKINFO observe_lastRun=$lastRun observe_result=$lastRes"

# ---- load today's records (RAW copy verbatim into this log for debugging) --
$lines = @(Get-Content $ObserveLog -ErrorAction SilentlyContinue |
    Where-Object { $_ -match "^$ForDate " })
$pre  = $lines | Where-Object { $_ -match "^$ForDate \d{2}:\d{2}:\d{2} PRE " }  | Select-Object -Last 1
$post = $lines | Where-Object { $_ -match "^$ForDate \d{2}:\d{2}:\d{2} POST " } | Select-Object -Last 1
if ($pre)  { Log "PRE_RAW $pre" }  else { Log "PRE_RAW <none for $ForDate>" }
if ($post) { Log "POST_RAW $post" } else { Log "POST_RAW <none for $ForDate>" }

# ---- SKIPPED guards (never page; each reason is logged) --------------------
$now = Get-Date
$today = $now.ToString('yyyy-MM-dd')
$skip = ''
if ($ForDate -eq $today -and (($now.Hour * 60 + $now.Minute) -lt (20 * 60 + 50))) {
    $skip = 'catch-up run before the 20:50 window'
}
if (-not $skip) {
    $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
    if ($boot.ToString('yyyy-MM-dd') -eq $ForDate -and
        (($boot.Hour * 60 + $boot.Minute) -gt (20 * 60 + 30))) {
        $skip = "machine booted at $($boot.ToString('HH:mm')) -- past the 20:30 window"
    }
}
if (-not $skip -and -not $pre -and -not $post -and (Test-GatewayNow)) {
    $skip = 'no PRE/POST records but gateway healthy now -- window missed (sleep/off) or observe task did not run'
}
if ($skip) {
    Log "SUMMARY result=SKIPPED checks=0 reason='$skip'"
    Send-Push 'info' "quant: 20:50 verify SKIPPED -- $skip"
    Log "END result=SKIPPED elapsed=$([int]((Get-Date) - $t0).TotalSeconds)s"
    exit 0
}

# ---- current truth at 20:50 (a restart that finished late is still healthy) --
# Real handshake, not port-open: 4001 can listen before login completes.
. 'D:\quant\scripts\windows\ib-api-handshake.ps1'
$hsOk = Test-IbApiHandshake -HostName '127.0.0.1' -Port 4001 -TimeoutSec 6
# the late-login rescue only applies to TODAY's run (20:34 POST may predate a login that
# finished by 20:50); on forensic -ForDate runs the POST record is authoritative
$hsForLogin = ($ForDate -eq $today) -and $hsOk
$isSunday = ([datetime]::ParseExact($ForDate, 'yyyy-MM-dd', $null)).DayOfWeek -eq 'Sunday'

# ---- checks ----------------------------------------------------------------
$checks = @()
function Add-Check($name, $pass, $obs) {
    $script:checks += [pscustomobject]@{ name = $name; pass = [bool]$pass; obs = $obs }
    $tag = if ($pass) { 'PASS' } else { 'FAIL' }
    Log "CHECK $name=$tag $obs"
}

$ranToday = $ti -and $ti.LastRunTime -and ($ti.LastRunTime.ToString('yyyy-MM-dd') -eq $ForDate)
Add-Check 'observe_task' ($ranToday -and $ti.LastTaskResult -eq 0) "lastRun=$lastRun result=$lastRes"
Add-Check 'pre_record' ([bool]$pre) $(if ($pre) { 'present' } else { 'MISSING -- no pre-restart archive/state capture' })

if ($pre -and $pre -match 'archived=(\S+)') {
    $archDir = $matches[1]
    Add-Check 'archive_dir' (Test-Path $archDir) $archDir
} else {
    Log "CHECK archive_dir=SKIP no pre record to derive the path from"
}
Add-Check 'post_record' ([bool]$post) $(if ($post) { 'present' } else { 'MISSING -- no post-restart record' })

$f = @{}
if ($post) {
    if ($post -match 'autorestart=(\S+)')            { $f['auto'] = $matches[1] }
    if ($post -match 'ibcPid=(\S+?)\(prev=([^)]*)\)'){ $f['ibcPid'] = $matches[1]; $f['prev'] = $matches[2] }
    if ($post -match 'restarted=(\S+)')              { $f['restart'] = $matches[1] }
    if ($post -match 'p4001=(\S*)')                  { $f['p4001'] = $matches[1] }
    if ($post -match 'p4003v4=(\S*)')                { $f['p4003'] = $matches[1] }
    if ($post -match "finding='([^']*)'")            { $f['finding'] = $matches[1] }
    if ($post -match 'loginCompleted=(\S+)')         { $f['login'] = $matches[1] }
    if ($post -match '2faInitiated=(\S+)')           { $f['twofa'] = $matches[1] }
    if ($post -match "securityWin='([^']*)'")        { $f['sec'] = $matches[1] }
    if ($post -match "cold='([^']*)'") {
        # Normalize BOTH record shapes to the bare "YYYY/MM/DD HH:MM": legacy
        # records stored the full gateway sentence ("Gateway will be cold
        # restarted at ..."), Get-ExpectedCold() returns the bare date, and a
        # strict -eq between them was the 2026-10-17 20:50 false page.
        $cv = $matches[1]
        if ($cv -match '(\d{4}/\d{2}/\d{2} \d{2}:\d{2})') { $cv = $matches[1] }
        $f['cold'] = $cv
    }
}
# Observed-value helper: NEVER `-or` (that yields booleans like "True" -- useless in a log).
# Missing whole POST record vs an empty field are different facts, so label them differently.
function P($k) {
    if (-not $post) { return 'NO-POST-RECORD' }
    $v = $f[$k]
    if (-not $v) { return '(empty)' }
    return $v
}
Add-Check 'restarted' ($f['restart'] -eq 'yes') "restarted=$(P 'restart') prev=$(P 'prev') -> new=$(P 'ibcPid')"
Add-Check 'port_4001_at_post' ([bool]$f['p4001']) "p4001=$(P 'p4001')"
Add-Check 'relay_4003_at_post' ([bool]$f['p4003']) "p4003v4=$(P 'p4003')"
if ($isSunday) {
    # Sunday = COLD restart by design: no token file, full passkey. Asserting
    # "file found" here would false-page the one planned touch of the week (P5).
    Log "CHECK token_finding=SKIP sunday-cold-night (full passkey by design) finding='$(P 'finding')'"
} else {
    Add-Check 'token_finding' ($f['finding'] -match 'autorestart file found') "finding='$(P 'finding')'"
}
# login by 20:50 counts even if POST at 20:34 caught it mid-restart (or, on Sunday, before
# the planned touch): a genuine handshake NOW is stronger evidence than the frozen record.
$loginNow = if ($f['login'] -eq 'yes') { 'confirmed-at-post' }
            elseif ($hsForLogin) { 'confirmed-by-verify(late)' }
            else { 'no' }
Add-Check 'login_completed' ($loginNow -ne 'no') "login=$loginNow handshakeAtVerify=$hsOk sameDay=$($ForDate -eq $today)"
$expectedCold = Get-ExpectedCold $ForDate
Add-Check 'cold_schedule' ($f['cold'] -and $f['cold'] -eq $expectedCold) "cold='$(P 'cold')' expected='$expectedCold'"
# a prompt still open at 20:34 is only "stuck" if the handshake is ALSO not up at 20:50
Add-Check 'no_stuck_prompt' ($f['sec'] -eq 'none' -or $hsForLogin) "securityWin='$(P 'sec')' handshakeAtVerify=$hsOk"
Add-Check 'gateway_now' ($hsOk -and (Get-IbcPids)) "handshake=$hsOk ibcPids=$((Get-IbcPids) -join ',')"

# observed, NOT asserted (token re-login semantics for "2FA initiated" are not yet pinned)
Log "OBSERVE autorestart_at_post=$(P 'auto') 2faInitiated=$(P 'twofa') cold=$(P 'cold') handshakeAtVerify=$hsOk sunday=$isSunday"

# ---- summary + one push ----------------------------------------------------
$failed = @($checks | Where-Object { -not $_.pass })
$result = if ($failed.Count -eq 0) { 'PASS' } else { 'FAIL' }
$failedNames = (($failed | ForEach-Object { $_.name }) -join ',')
Log "SUMMARY result=$result checks=$($checks.Count) failed=$($failed.Count) list=[$failedNames]"

if ($result -eq 'PASS') {
    $what = if ($isSunday) {
        "Sunday cold restart done, login ($loginNow), next cold $(P 'cold')"
    } else {
        "token restart ok $(P 'prev')->$(P 'ibcPid'), login done, cold $(P 'cold')"
    }
    Send-Push 'error' "quant: 20:50 verify PASS -- $what"
} else {
    Send-Push 'critical' "quant: 20:50 verify FAIL ($($failed.Count): $failedNames) -- see C:\IBC-Live\Logs\restart-verify.log"
}
Log "END result=$result elapsed=$([int]((Get-Date) - $t0).TotalSeconds)s"
exit 0
