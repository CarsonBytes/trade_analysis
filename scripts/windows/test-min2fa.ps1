# Test suite for the MIN-2FA stack + operational guards (2026-10-17).
# Coverage: script parse checks, live handshake, gateway-push level matrix (offline,
# cannot page), port ownership, watchdog liveness, WU policy, task triggers/states,
# log structure, synthetic restart-verify matrix (-NoPush), WSL cron/wrapper.
# Safe to run any time: no test sends a Telegram/ntfy message.
#   powershell -File scripts\windows\test-min2fa.ps1
# Exit code = number of failed checks.
param()
$ErrorActionPreference = 'Continue'
$repo = 'D:\quant'
$VerifyLog = 'C:\IBC-Live\Logs\restart-verify.log'
$ObserveLog = 'C:\IBC-Live\Logs\restart-observe.log'
$script:R = @()

function TC([string]$name, [scriptblock]$b) {
    $st = 'PASS'; $d = ''
    try {
        $out = & $b
        if ($null -ne $out -and "$out" -ne '') { $d = "$out" -replace "`r?`n", ' ' }
    } catch { $st = 'FAIL'; $d = $_.Exception.Message }
    if ($st -eq 'PASS') { $d = '' }   # keep table clean; detail only on fail
    $script:R += [pscustomobject]@{ Name = $name; Status = $st; Detail = $d }
    Write-Host ("[{0}] {1}{2}" -f $st, $name, $(if ($d) { " -- $d" } else { '' }))
}

# ---- 1. parse checks --------------------------------------------------------
foreach ($f in @('watchdog.ps1', 'scripts\windows\restart-observe.ps1',
        'scripts\windows\restart-verify.ps1', 'scripts\windows\ib-api-handshake.ps1',
        'scripts\windows\ibkr-live-autostart.ps1', 'scripts\windows\check-autorestart.ps1')) {
    $path = Join-Path $repo $f
    TC "parse $f" {
        $errs = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$errs)
        if ($errs.Count) { throw (($errs | ForEach-Object { $_.Message }) -join '; ') }
        'ok'
    }
}

# ---- 2. live handshake ------------------------------------------------------
. (Join-Path $repo 'scripts\windows\ib-api-handshake.ps1')
TC 'handshake positive 127.0.0.1:4001' {
    $r = Test-IbApiHandshake -HostName '127.0.0.1' -Port 4001 -TimeoutSec 6
    if (-not $r) { throw 'expected $true (gateway/IBC down -- or handshake regression)' }
    'true'
}
TC 'handshake negative closed port 65530' {
    $r = Test-IbApiHandshake -HostName '127.0.0.1' -Port 65530 -TimeoutSec 2
    if ($r) { throw 'expected $false for a closed port' }
    'false'
}

# ---- 3. push level matrix (offline, WSL) ------------------------------------
TC 'gateway-push level matrix (offline)' {
    $out = (& wsl.exe -e bash /mnt/d/quant/scripts/gateway_push_test.sh) -join "`n"
    if ($LASTEXITCODE -ne 0) {
        $bad = ($out -split "`n") | Where-Object { $_ -match '^FAIL' }
        throw "exit=${LASTEXITCODE}: $($bad -join ' | ')"
    }
    ([regex]::Matches($out, '(?m)^PASS ')).Count.ToString() + ' offline cases pass'
}

# ---- 4. port ownership ------------------------------------------------------
TC 'port 4001 listener owned by java (IBC)' {
    $c = Get-NetTCPConnection -LocalPort 4001 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { throw 'nothing listening on 4001' }
    $pn = (Get-Process -Id $c.OwningProcess -ErrorAction Stop).ProcessName
    if ($pn -ne 'java') { throw "owner=$pn (expected java)" }
    $pn
}
TC 'port 4003 relay listener on 0.0.0.0' {
    $cs = @(Get-NetTCPConnection -LocalPort 4003 -State Listen -ErrorAction SilentlyContinue)
    if (-not $cs) { throw 'nothing listening on 4003' }
    $bind = @($cs | Where-Object { $_.LocalAddress -eq '0.0.0.0' })
    if (-not $bind) { throw "only bound to $(($cs | ForEach-Object LocalAddress) -join ',')" }
    $owner = (Get-Process -Id $bind[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
    if ($owner -and $owner -notmatch 'powershell') { throw "owner=$owner (expected powershell relay)" }
    "0.0.0.0 owner=$owner"
}

# ---- 5. watchdog liveness ---------------------------------------------------
TC 'watchdog process alive' {
    $p = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -match 'watchdog\.ps1' })
    if (-not $p) { throw 'no watchdog process' }
    "pid=$($p[0].ProcessId)"
}
TC 'watchdog pid file matches live process' {
    $pidW = [int]((Get-Content "$repo\logs\watchdog.pid" -ErrorAction Stop) -join '').Trim()
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$pidW" -ErrorAction SilentlyContinue
    if (-not $proc -or $proc.CommandLine -notmatch 'watchdog\.ps1') { throw "pid $pidW is not the watchdog" }
    $pidW
}

# ---- 6. Windows Update policy (P2) ------------------------------------------
TC 'NoAutoRebootWithLoggedOnUsers=1' {
    $v = (Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' -ErrorAction Stop).NoAutoRebootWithLoggedOnUsers
    if ($v -ne 1) { throw "value=$v (expected 1)" }
    '1 (WindowsUpdate\AU)'
}

# ---- 7. scheduled tasks -----------------------------------------------------
function Get-TriggerTimes($t) { @($t.Triggers | ForEach-Object { ([datetime]$_.StartBoundary).ToString('HH:mm') }) }
function Test-DailyTimes($taskPath, $taskName, [string[]]$want) {
    $t = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop
    $times = Get-TriggerTimes $t
    $missing = @($want | Where-Object { $times -notcontains $_ })
    if ($missing) { throw "missing trigger(s) $($missing -join ',') -- have: $($times -join ',')" }
    $dailies = @($t.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskDailyTrigger' -and $_.DaysInterval -eq 1 })
    if ($dailies.Count -lt $want.Count) { throw "only $($dailies.Count) daily trigger(s), want $($want.Count)" }
    if ($t.State -eq 'Disabled') { throw 'task disabled' }
    "$($want -join ',') daily state=$($t.State)"
}
TC 'task \IBKR\Restart Observe daily 20:29+20:34' { Test-DailyTimes '\IBKR\' 'Restart Observe' @('20:29', '20:34') }
TC 'task \IBKR\Restart Verify daily 20:50' { Test-DailyTimes '\IBKR\' 'Restart Verify' @('20:50') }
TC 'task Docker Daily Restart at 20:45' {
    $t = Get-ScheduledTask -TaskName 'Docker Daily Restart' -ErrorAction Stop
    $times = Get-TriggerTimes $t
    if ($times -notcontains '20:45') { throw "times=$($times -join ',')" }
    if ($t.State -eq 'Disabled') { throw 'disabled' }
    "$($times -join ',') state=$($t.State)"
}
TC 'task Docker Daily Prune at 04:00' {
    $t = Get-ScheduledTask -TaskName 'Docker Daily Prune' -ErrorAction Stop
    $times = Get-TriggerTimes $t
    if ($times -notcontains '04:00') { throw "times=$($times -join ',')" }
    if ($t.State -eq 'Disabled') { throw 'disabled' }
    "$($times -join ',') state=$($t.State)"
}
TC 'task \IBKR\API Relay running' {
    # 2026-10-18: the task's action is wscript + win-api-relay.vbs (detached hidden spawn,
    # console-close-proof). wscript exits immediately, so the engine state settles at
    # 'Ready' while the relay powershell lives on -- that IS the healthy steady state, and
    # asserting -eq 'Running' here failed for weeks. Ground truth is the listener itself
    # (check 'port 4003 relay listener' above); this check only guards the task existing
    # and not being disabled, which is what watchdog.ps1's Start-ScheduledTask needs.
    $t = Get-ScheduledTask -TaskPath '\IBKR\' -TaskName 'API Relay' -ErrorAction Stop
    if ($t.State -eq 'Disabled') { throw 'disabled' }
    $l = @(Get-NetTCPConnection -LocalPort 4003 -State Listen -ErrorAction SilentlyContinue |
           Where-Object { $_.LocalAddress -eq '0.0.0.0' })
    if (-not $l) { throw "state=$($t.State) and no 0.0.0.0:4003 listener" }
    "state=$($t.State) listener=present"
}

# ---- 8. log structure -------------------------------------------------------
TC 'observe log has START/PRE/POST/END records' {
    $tail = Get-Content $ObserveLog -Tail 300 -ErrorAction Stop
    foreach ($v in @('START', 'PRE', 'POST', 'END')) {
        $hit = $tail | Where-Object { $_ -match "^20\d\d-\d\d-\d\d \d\d:\d\d:\d\d $v " }
        if (-not $hit) { throw "no '$v' line in last 300" }
    }
    'all four verbs present'
}
TC 'verify log has START/TASKINFO/PRE_RAW/CHECK/SUMMARY/PUSH/END' {
    $tail = Get-Content $VerifyLog -Tail 300 -ErrorAction Stop
    foreach ($v in @('START ', 'TASKINFO ', 'PRE_RAW ', 'CHECK ', 'SUMMARY ', 'PUSH_SKIPPED', 'END ')) {
        if (-not ($tail | Where-Object { $_ -match [regex]::Escape($v) })) { throw "no '$($v.Trim())' line in last 300" }
    }
    'all seven markers present'
}

# ---- 9. synthetic restart-verify matrix (-NoPush) ---------------------------
$SynHealthy = "$repo\tmp\syn_verify_healthy.log"
$SynBroken = "$repo\tmp\syn_verify_broken.log"
@(
    "2026-10-16 20:29:02 PRE autorestart=absent ibcPids=[7000] p4001=127.0.0.1:7000 p4003v4=0.0.0.0:28192 securityWin='none' archived=C:\IBC-Live\Logs\archived [IBC-3.24.0_GATEWAY-1047_.txt, launcher.log]"
    "2026-10-16 20:34:05 POST autorestart=absent ibcPid=7100(prev=7000) restarted=yes p4001=127.0.0.1:7100 p4003v4=0.0.0.0:28192 finding='autorestart file found C:\Jts-Live\ajanink autorestart' loginCompleted=yes 2faInitiated=no securityWin='none' cold='Gateway will be cold restarted at 2026/10/18 20:30'"
    "2026-10-11 20:29:02 PRE autorestart=absent ibcPids=[7000] p4001=127.0.0.1:7000 p4003v4=0.0.0.0:28192 securityWin='none' archived=C:\IBC-Live\Logs\archived [IBC-3.24.0_GATEWAY-1047_.txt, launcher.log]"
    "2026-10-11 20:34:05 POST autorestart=absent ibcPid=7200(prev=7000) restarted=yes p4001=127.0.0.1:7200 p4003v4=0.0.0.0:28192 finding='autorestart file not found: full authentication will be required' loginCompleted=no 2faInitiated=no securityWin='none' cold='2026/10/18 20:30'"
) | Set-Content -Path $SynHealthy -Encoding UTF8
@(
    "2026-10-16 20:29:02 PRE autorestart=absent ibcPids=[7000] p4001=127.0.0.1:7000 p4003v4=0.0.0.0:28192 securityWin='none' archived=C:\IBC-Live\Logs\archived [IBC-3.24.0_GATEWAY-1047_.txt, launcher.log]"
    "2026-10-16 20:34:05 POST autorestart=absent ibcPid=7100(prev=7000) restarted=yes p4001=127.0.0.1:7100 p4003v4=0.0.0.0:28192 finding='autorestart file found C:\Jts-Live\ajanink autorestart' loginCompleted=no 2faInitiated=no securityWin='none' cold='2026/10/18 20:30'"
) | Set-Content -Path $SynBroken -Encoding UTF8

function Invoke-VerifySynthetic([string]$forDate, [string]$synFile) {
    $before = @(Get-Content $VerifyLog -ErrorAction Stop).Count
    & "$repo\scripts\windows\restart-verify.ps1" -ForDate $forDate -ObserveLog $synFile -NoPush *>> $null
    $new = @(Get-Content $VerifyLog | Select-Object -Skip $before)
    return $new
}

TC 'verify synthetic: healthy Friday (only observe_task fails)' {
    $new = Invoke-VerifySynthetic '2026-10-16' $SynHealthy
    $sum = $new | Where-Object { $_ -match 'SUMMARY ' } | Select-Object -Last 1
    if ($sum -notmatch 'failed=1 list=\[observe_task\]') { throw "summary: $sum" }
    foreach ($c in @('pre_record=PASS', 'post_record=PASS', 'restarted=PASS', 'token_finding=PASS',
            'login_completed=PASS', 'cold_schedule=PASS', 'no_stuck_prompt=PASS', 'gateway_now=PASS')) {
        if (-not ($new | Where-Object { $_ -match [regex]::Escape("CHECK $c") })) { throw "missing CHECK $c" }
    }
    if (-not ($new | Where-Object { $_ -match 'PUSH_SKIPPED\(-NoPush\) level=critical' })) { throw 'no critical PUSH_SKIPPED line' }
    'all checks PASS except observe_task (lastRun semantics), push marked SKIPPED'
}
TC 'verify synthetic: broken login fails login_completed' {
    $new = Invoke-VerifySynthetic '2026-10-16' $SynBroken
    $sum = $new | Where-Object { $_ -match 'SUMMARY ' } | Select-Object -Last 1
    if ($sum -notmatch 'failed=2 list=\[observe_task,login_completed\]') { throw "summary: $sum" }
    if (-not ($new | Where-Object { $_ -match 'CHECK login_completed=FAIL' })) { throw 'missing login_completed=FAIL' }
    $sum
}
TC 'verify synthetic: Sunday cold night skips token_finding' {
    $new = Invoke-VerifySynthetic '2026-10-11' $SynHealthy
    if (-not ($new | Where-Object { $_ -match 'CHECK token_finding=SKIP sunday-cold-night' })) { throw 'missing token_finding SKIP' }
    if (-not ($new | Where-Object { $_ -match 'CHECK cold_schedule=PASS' })) { throw 'cold_schedule should still PASS on Sunday' }
    if (-not ($new | Where-Object { $_ -match 'OBSERVE .*sunday=True' })) { throw 'OBSERVE should log sunday=True' }
    'token_finding skipped, cold_schedule observed'
}
TC 'verify synthetic: catch-up before 20:50 is SKIPPED+info' {
    $new = Invoke-VerifySynthetic (Get-Date -Format 'yyyy-MM-dd') "$repo\tmp\syn_verify_healthy.log"
    $sum = $new | Where-Object { $_ -match 'SUMMARY ' } | Select-Object -Last 1
    if ($sum -notmatch 'result=SKIPPED') { throw "summary: $sum" }
    if (-not ($new | Where-Object { $_ -match "PUSH_SKIPPED\(-NoPush\) level=info" })) { throw 'skip must push info, not critical' }
    $sum
}

# ---- 10. WSL cron + wrapper --------------------------------------------------
TC 'WSL crontab has weekly flex reconcile entry' {
    $o = (& wsl.exe -e bash -c "crontab -l 2>/dev/null | grep -c flex_nav_reconcile_weekly") -join ''
    if ([int]$o -lt 1) { throw "grep -c=$o" }
    'present'
}
TC 'flex wrapper syntax (bash -n)' {
    $o = (& wsl.exe -e bash -c "bash -n /mnt/d/quant/scripts/flex_nav_reconcile_weekly.sh && echo OK") -join ''
    if ($o -ne 'OK') { throw "out=$o" }
    'OK'
}
TC 'flex reconcile compiles (py_compile in container)' {
    $o = (& wsl.exe -e bash -c "docker exec quant-dashboard-live-docker /app/.venv/bin/python3 -m py_compile /tmp/flex_nav_reconcile.py 2>&1; echo RC=`$?") -join ''
    if ($o -notmatch 'RC=0') {
        # script may have been cleaned from /tmp -- compile the repo copy instead
        $o2 = (& wsl.exe -e bash -c "python3 -m py_compile /mnt/d/quant/scripts/flex_nav_reconcile.py 2>&1; echo RC=`$?") -join ''
        if ($o2 -notmatch 'RC=0') { throw "container: $o ; wsl: $o2" }
    }
    'compiled'
}

# ---- summary ----------------------------------------------------------------
$script:R | Format-Table -AutoSize -Wrap
$nf = @($script:R | Where-Object { $_.Status -eq 'FAIL' }).Count
Write-Host ("TOTAL: {0} checks, {1} failed" -f $script:R.Count, $nf)
exit $nf
