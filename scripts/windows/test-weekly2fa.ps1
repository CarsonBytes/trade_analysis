# Regression suite for IBKR_WEEKLY_2FA_SPEC (2026-11-21). Supersedes test-min2fa.ps1, whose
# trigger-time checks assumed a Windows clock in HKT and whose observe/verify chain is retired.
# Safe to run any time: never logs in, never kills IBC, never pages (push cases are offline).
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\test-weekly2fa.ps1
# Exit code = number of failed checks.
param()
$ErrorActionPreference = 'Continue'
$repo = 'D:\quant'
$win = Join-Path $repo 'scripts\windows'
$arch = 'C:\IBC-Live\Logs\archived'
. (Join-Path $win 'ibkr-common.ps1')
. (Join-Path $win 'ib-api-handshake.ps1')
$global:IbkrNoPush = $true
$script:R = @()
function TC([string]$name, [scriptblock]$b) {
    $st = 'PASS'; $d = ''
    try { $out = & $b; if ($null -ne $out) { $d = "$out" -replace "`r?`n", ' ' } } catch { $st = 'FAIL'; $d = $_.Exception.Message }
    $script:R += [pscustomobject]@{ Name = $name; Status = $st; Detail = $d }
    Write-Host ("[{0}] {1}{2}" -f $st, $name, $(if ($d) { " -- $d" } else { '' }))
}
function Need($cond, $why) { if (-not $cond) { throw $why } }
function Lines($dir, $file) { @(Get-Content (Join-Path (Join-Path $arch $dir) $file) -ErrorAction Stop) }
function Ast($f) { $e = $null; [System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$null, [ref]$e) }

# ---- 1. parse --------------------------------------------------------------------------
foreach ($f in @('watchdog.ps1', 'scripts\windows\ibkr-common.ps1', 'scripts\windows\ibkr-live-login.ps1',
        'scripts\windows\ibkr-live-autostart.ps1', 'scripts\windows\weekly-touch.ps1',
        'scripts\windows\restart-live-gateway.ps1', 'scripts\windows\ib-api-handshake.ps1')) {
    $p = Join-Path $repo $f
    TC "parse $f" { $e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$null, [ref]$e); Need ($e.Count -eq 0) (($e | ForEach-Object Message) -join '; ') }
}

# ---- 2. static invariants ----------------------------------------------------------------
TC 'config.ini: ColdRestartTime disabled (§3.3)' {
    $l = Select-String -Path 'C:\IBC-Live\config.ini' -Pattern '^ColdRestartTime=' | Select-Object -First 1
    Need ($l -and $l.Line -eq 'ColdRestartTime=') "found: $($l.Line)"
}
TC 'config.ini: AutoRestartTime outside US RTH (HKT)' {
    $l = (Select-String -Path 'C:\IBC-Live\config.ini' -Pattern '^AutoRestartTime=(.+)$').Matches[0].Groups[1].Value.Trim()
    $t = [datetime]::ParseExact($l, 'hh:mm tt', [Globalization.CultureInfo]::InvariantCulture)
    $m = $t.Hour * 60 + $t.Minute
    Need ($m -ge 5 * 60 -and $m -le 21 * 60 + 15) "AutoRestartTime=$l is inside 21:30-05:00 HKT"
    $l
}
TC 'router reboot >= 10 min away from the gateway daily restart (RETRO §3.2 H3)' {
    # Oct 23 / Oct 27: router offline 05:30:01-05:31 during the 05:30 warm restart -> 2 touches.
    # Longest router outage logged: 102 s. 10 min margin; the user chose gateway 05:45 / router 05:30.
    # Router tasks are UTC-anchored; the launcher skips any fire before 05:00 HKT.
    $l = (Select-String -Path 'C:\IBC-Live\config.ini' -Pattern '^AutoRestartTime=(.+)$').Matches[0].Groups[1].Value.Trim()
    $g = [datetime]::ParseExact($l, 'hh:mm tt', [Globalization.CultureInfo]::InvariantCulture)
    $gm = $g.Hour * 60 + $g.Minute
    $fires = @(Get-ScheduledTask -TaskName 'Xiaomi Router Restart Daily*' -ErrorAction Stop | Where-Object State -ne 'Disabled' |
        ForEach-Object { $_.Triggers } | ForEach-Object { ([datetimeoffset]::Parse($_.StartBoundary)).UtcDateTime.AddHours(8) } |
        Where-Object { $_.Hour -ge 5 } | ForEach-Object { $_.Hour * 60 + $_.Minute })
    Need ($fires.Count -ge 1) 'no router reboot fire at/after 05:00 HKT found'
    foreach ($r in $fires) {
        $d = [math]::Abs($r - $gm); $d = [math]::Min($d, 1440 - $d)
        Need ($d -ge 10) ("router reboot {0:00}:{1:00} HKT is {2} min from AutoRestartTime={3}" -f [math]::Floor($r / 60), ($r % 60), $d, $l)
    }
    "router {0} HKT vs gateway {1}" -f (($fires | ForEach-Object { '{0:00}:{1:00}' -f [math]::Floor($_ / 60), ($_ % 60) }) -join ','), $l
}
TC 'router launcher guard is HKT (dry run: 04:30 skip, 05:30 reboot)' {
    $v = 'D:\claude\xiaomi-router-reboot.vbs'
    $a = (& cscript.exe //nologo $v /dryrun /hkt:2026-11-22T04:30) -join ' '
    $b = (& cscript.exe //nologo $v /dryrun /hkt:2026-11-22T05:30) -join ' '
    Need ($a -match 'HKT SKIP pre-05:00') "04:30: $a"
    Need ($b -match 'HKT WOULD REBOOT') "05:30: $b"
}
TC 'gateway JVM pinned to HKT (vmoptions)' {
    Need ([bool](Select-String -Path 'C:\Jts\ibgateway\1047\ibgateway.vmoptions' -Pattern '^-Duser\.timezone=Asia/Hong_Kong$')) 'missing -Duser.timezone=Asia/Hong_Kong'
}
TC 'login: every MessageBox is inside Ask(), and Ask() returns early under -Unattended' {
    $a = Ast (Join-Path $win 'ibkr-live-login.ps1')
    $shows = $a.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and "$($n.Expression)" -match 'MessageBox' }, $true)
    foreach ($s in $shows) {
        $p = $s.Parent; while ($p -and -not ($p -is [System.Management.Automation.Language.FunctionDefinitionAst])) { $p = $p.Parent }
        Need ($p -and $p.Name -eq 'Ask') "MessageBox outside Ask() at line $($s.Extent.StartLineNumber)"
    }
    $ask = $a.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Ask' }, $true)
    Need ($ask.Body.Extent.Text -match 'if \(\$Unattended\) \{ return') 'Ask() does not short-circuit on -Unattended'
    Need (-not ($a.Extent.Text -match 'Read-Host')) 'Read-Host present'
    "$($shows.Count) MessageBox site(s), all in Ask()"
}
TC 'autostart + weekly-touch: no MessageBox / Read-Host at all' {
    foreach ($f in 'ibkr-live-autostart.ps1', 'weekly-touch.ps1') {
        $t = Get-Content (Join-Path $win $f) -Raw
        Need (-not ($t -match 'MessageBox\]::Show|Read-Host')) "$f has a modal"
    }
}
TC 'watchdog launches login hidden + -Unattended' {
    $a = Ast (Join-Path $repo 'watchdog.ps1')
    $fn = $a.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Start-LiveLogin' }, $true)
    Need ($fn.Body.Extent.Text -match '-WindowStyle Hidden') 'not hidden'
    Need ($fn.Body.Extent.Text -match "'-Unattended'") 'no -Unattended'
}
TC 'no local-clock log stamps in the gateway scripts' {
    $bad = @()
    foreach ($f in 'watchdog.ps1', 'scripts\windows\ibkr-live-login.ps1', 'scripts\windows\ibkr-live-autostart.ps1',
            'scripts\windows\weekly-touch.ps1', 'scripts\windows\restart-live-gateway.ps1') {
        $t = Get-Content (Join-Path $repo $f) -Raw
        if ($t -match "Get-Date -Format 'yyyy|-f \(Get-Date\)|\(Get-Date\)\.Hour") { $bad += $f }
    }
    Need ($bad.Count -eq 0) ($bad -join ', ')
}

# ---- 3. Docker live gateway parked (§3.2) --------------------------------------------------
TC 'docker live gateway: restart=no, not running, hold file' {
    $o = (& wsl.exe -e bash -lc "docker inspect -f '{{.State.Status}} {{.HostConfig.RestartPolicy.Name}}' quant-ibgateway-live-docker 2>/dev/null || echo absent; test -f /home/cap/.container-hold/quant-ibgateway-live-docker && echo held") -join ' '
    Need ($o -match 'absent' -or ($o -match '^(exited|created) no' -and $o -match 'held')) $o
    $o
}
TC 'compose: live docker gateway has no LIVE credentials + restart no' {
    $t = Get-Content (Join-Path $repo 'docker-compose.live.yml') -Raw
    $blk = [regex]::Match($t, '(?ms)^  ib-gateway-live:.*?(?=^  \S|\z)').Value
    Need ($blk -match 'restart: "no"') 'restart is not "no"'
    Need (-not ($blk -match '(?m)^\s+- \.env\.live')) '.env.live still wired in'
}

# ---- 4. tasks ---------------------------------------------------------------------------
TC 'retired tasks disabled (Restart Observe / Restart Verify)' {
    foreach ($n in 'Restart Observe', 'Restart Verify') {
        $t = Get-ScheduledTask -TaskPath '\IBKR\' -TaskName $n -ErrorAction SilentlyContinue
        Need (-not $t -or $t.State -eq 'Disabled') "$n is $($t.State)"
    }
}
TC 'no failing enabled \IBKR\ task (watchdog would page)' {
    . { function Write-Log { } }
    $a = Ast (Join-Path $repo 'watchdog.ps1')
    $fn = $a.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Test-IbkrTasks' }, $true)
    Invoke-Expression $fn.Extent.Text
    $bad = @(Test-IbkrTasks); Need ($bad.Count -eq 0) ($bad -join ', ')
}

# ---- 5. classifier + ledger fixtures (archived real logs) ----------------------------------
$fx = @(
    @{ d = '20261103_203157_login'; age = 100; want = 'token-rejected' },   # Nov 3 20:30 token refused
    @{ d = '20261120_043159_login'; age = 100; want = 'token-rejected' },   # Nov 20 20:30 token refused
    @{ d = '20261024_053205_login'; age = 120; want = 'starting' },         # Oct 24 05:30 restart, completed 05:32:38
    @{ d = '20261120_233113_login'; age = 600; want = 'logged-out' }        # today: 2FA closed, logged out
)
foreach ($f in $fx) {
    TC "Get-IbcState $($f.d) -> $($f.want)" {
        $s = Get-IbcState -IbcLines (Lines $f.d 'IBC-3.24.0_GATEWAY-1047_.txt') -LauncherLines (Lines $f.d 'launcher.log') -JavaAgeSec $f.age -PromptOpen 0 -ProcCount 1
        Need ($s -eq $f.want) "got $s"
    }
}
TC 'Get-IbcState: no process -> none; open prompt -> prompt-pending' {
    Need ((Get-IbcState -IbcLines @() -LauncherLines @() -ProcCount 0) -eq 'none') 'none'
    Need ((Get-IbcState -IbcLines @() -LauncherLines @() -ProcCount 1 -PromptOpen 1) -eq 'prompt-pending') 'pending'
}
TC 'auth events: Nov 19 full auth then Nov 20 token restart (no login)' {
    $ev = @(Get-IbcAuthEvents (Lines '20261120_043159_login' 'IBC-3.24.0_GATEWAY-1047_.txt'))
    $full = @($ev | Where-Object Kind -eq 'full-auth')
    Need ($full.Count -eq 1 -and $full[0].Ts -eq [datetime]'2026-11-19 21:48:24') ("got " + (($ev | ForEach-Object { "$($_.Kind)@$($_.Ts)" }) -join ','))
}
TC 'auth events: Nov 9 full auth + 20:30 token login' {
    $ev = @(Get-IbcAuthEvents (Lines '20261110_042907_login' 'IBC-3.24.0_GATEWAY-1047_.txt'))
    $k = ($ev | ForEach-Object Kind) -join ','
    Need ($k -eq 'full-auth,token-login') "got $k"
}
TC 'warm verdicts: Nov 3 rejected, Nov 9 accepted' {
    $a = @(Get-WarmVerdicts (Lines '20261103_203157_login' 'launcher.log'))
    $b = @(Get-WarmVerdicts (Lines '20261110_042907_login' 'launcher.log'))
    Need ($a.Count -ge 1 -and $a[-1].Verdict -eq 'rejected') "Nov3: $(($a | ForEach-Object Verdict) -join ',')"
    Need ($b.Count -ge 1 -and $b[-1].Verdict -eq 'accepted') "Nov9: $(($b | ForEach-Object Verdict) -join ',')"
}
TC 'ledger: first full auth after the Sunday boundary is planned, the next one is not; idempotent' {
    $tmp = New-Item -ItemType Directory -Force -Path (Join-Path $env:TEMP "w2fa_$PID")
    $saved = $script:LedgerFile, $script:WeeklyState, $script:DownState
    $script:LedgerFile = Join-Path $tmp 'l.jsonl'; $script:WeeklyState = Join-Path $tmp 's.json'; $script:DownState = Join-Path $tmp 'd.json'
    try {
        $lines = @('2026-11-15 22:03:56:458 IBC: Second Factor Authentication initiated', '2026-11-15 22:04:19:044 IBC: Login has completed',
                   '2026-11-15 20:30:11:158 IBC: Starting Gateway',
                   '2026-11-16 12:32:22:918 IBC: Second Factor Authentication initiated', '2026-11-16 12:33:18:663 IBC: Login has completed')
        $n1 = @(Update-TouchLedger -IbcLines $lines -LauncherLines @())
        $n2 = @(Update-TouchLedger -IbcLines $lines -LauncherLines @())
        Need ($n1.Count -eq 2) "first scan added $($n1.Count)"
        Need ($n2.Count -eq 0) "second scan added $($n2.Count) (not idempotent)"
        Need ($n1[0].planned -eq $true -and $n1[1].planned -eq $false) "planned flags: $($n1[0].planned),$($n1[1].planned)"
        Need ($n1[0].week -eq '2026-11-15') "week=$($n1[0].week)"
    } finally { $script:LedgerFile, $script:WeeklyState, $script:DownState = $saved; Remove-Item $tmp -Recurse -Force }
}

# ---- 6. weekly window (injected HKT clock) ---------------------------------------------------
$empty = [pscustomobject]@{ reminderWeek = ''; touchWeek = ''; touchLaunchedAt = ''; reportWeek = '' }
$cases = @(
    @{ t = '2026-11-21 05:59'; hold = $false; due = '' },
    @{ t = '2026-11-21 06:00'; hold = $true;  due = '' },
    @{ t = '2026-11-21 16:45'; hold = $true;  due = '' },
    @{ t = '2026-11-22 20:26'; hold = $true;  due = 'reminder' },
    @{ t = '2026-11-22 20:36'; hold = $false; due = 'touch' },
    @{ t = '2026-11-22 23:40'; hold = $false; due = '' },
    @{ t = '2026-11-23 09:05'; hold = $false; due = 'report' },
    @{ t = '2026-11-20 12:00'; hold = $false; due = '' }
)
foreach ($c in $cases) {
    TC "weekly window @ $($c.t) HKT: hold=$($c.hold) due='$($c.due)'" {
        $now = [datetime]$c.t
        $w = Get-WeeklyWindow $now
        $d = Get-DueWeeklyPhase $now $empty
        Need ($w.InHold -eq $c.hold) "hold=$($w.InHold)"
        Need ($d -eq $c.due) "due='$d'"
    }
}
TC 'weekly window: done phases are not re-dispatched' {
    $done = [pscustomobject]@{ reminderWeek = '2026-11-22'; touchWeek = '2026-11-22'; touchLaunchedAt = ''; reportWeek = '2026-11-23' }
    foreach ($t in '2026-11-22 20:26', '2026-11-22 20:36', '2026-11-23 09:05') { $d = Get-DueWeeklyPhase ([datetime]$t) $done; Need ($d -eq '') "$t -> '$d'" }
}
TC 'clock is HKT from UTC, independent of the Windows timezone' {
    $global:IbkrNowOverride = $null
    $diff = [math]::Abs(((Get-HktNow) - [DateTime]::UtcNow.AddHours(8)).TotalSeconds)
    Need ($diff -lt 2) "Get-HktNow off by ${diff}s"
    "Windows TZ=$((Get-TimeZone).Id); HKT now=$(Format-Hkt (Get-HktNow))"
}

# ---- 7. throttle + lock -------------------------------------------------------------------
TC 'launch budget: 3 launches in 30 min -> refused' {
    $saved = $script:LaunchFile; $script:LaunchFile = Join-Path $env:TEMP "w2fa_launch_$PID.txt"
    try {
        Need (Test-LaunchBudget) 'empty budget refused'
        1..3 | ForEach-Object { Register-IbcLaunch 'test' }
        Need (-not (Test-LaunchBudget)) '4th launch allowed'
    } finally { Remove-Item $script:LaunchFile -Force -ErrorAction SilentlyContinue; $script:LaunchFile = $saved }
}
TC 'login lock is exclusive across processes' {
    $m = Enter-LoginLock
    Need $m 'could not take the lock (a login run is active?)'
    try {
        $other = & powershell.exe -NoProfile -Command ". 'D:\quant\scripts\windows\ibkr-common.ps1'; if (Enter-LoginLock) { 'GOT' } else { 'REFUSED' }"
        Need ("$other" -match 'REFUSED') "second process: $other"
    } finally { $m.ReleaseMutex(); $m.Dispose() }
}

# ---- 8. push matrix (offline) + weekly-touch dry run ---------------------------------------------
TC 'gateway-push matrix incl. gateway cap exemption (offline)' {
    $out = (& wsl.exe -e bash /mnt/d/quant/scripts/gateway_push_test.sh) -join "`n"
    Need ($LASTEXITCODE -eq 0) (($out -split "`n" | Where-Object { $_ -match '^FAIL' }) -join ' | ')
    ([regex]::Matches($out, '(?m)^PASS ')).Count.ToString() + ' cases'
}
TC 'weekly-touch -DryRun decides and changes nothing' {
    $before = if (Test-Path $script:WeeklyState) { Get-Content $script:WeeklyState -Raw } else { '' }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $win 'weekly-touch.ps1') -Phase touch -DryRun | Out-Null
    Need ($LASTEXITCODE -eq 0) "exit $LASTEXITCODE"
    $after = if (Test-Path $script:WeeklyState) { Get-Content $script:WeeklyState -Raw } else { '' }
    Need ($before -eq $after) 'state file changed'
    $last = Get-Content 'C:\IBC-Live\Logs\weekly-touch.log' -Tail 1
    Need ($last -match 'decision=(login|cold|skip)') "log: $last"
    $last
}

# ---- 9. live health --------------------------------------------------------------------------
TC 'watchdog alive and logging in HKT' {
    $wpid = Get-Content 'D:\quant\logs\watchdog.pid' -ErrorAction Stop
    Need (Get-Process -Id $wpid -ErrorAction SilentlyContinue) "pid $wpid not running"
    $last = Get-Content 'D:\quant\logs\watchdog.log' -Tail 1
    Need ($last -match '^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) HKT ') "last line not HKT-stamped: $last"
    $age = ((Get-HktNow) - [datetime]$Matches[1]).TotalMinutes
    Need ($age -ge -1) "last HKT stamp is ${age} min in the future (clock/zone wrong)"
    # The watchdog logs on state CHANGES only, so a quiet log (e.g. weekend hold) is normal.
    # Liveness = the process is still burning CPU across two of its 20 s cycles.
    $c0 = (Get-Process -Id $wpid).TotalProcessorTime; Start-Sleep -Seconds 45
    $c1 = (Get-Process -Id $wpid).TotalProcessorTime
    Need ($c1 -gt $c0) "pid $wpid used no CPU in 45 s (hung?)"
    "pid $wpid cycling; last log line {0:N0} min ago" -f $age
}
TC 'handshake negative on a closed port' { Need (-not (Test-IbApiHandshake -HostName '127.0.0.1' -Port 65530 -TimeoutSec 2)) 'true on closed port' }
TC 'API relay listening on 0.0.0.0:4003' { Need ([bool](Get-NetTCPConnection -LocalPort 4003 -State Listen -ErrorAction SilentlyContinue | Where-Object LocalAddress -eq '0.0.0.0')) 'no listener' }

$fails = @($script:R | Where-Object Status -eq 'FAIL').Count
Write-Host ("WEEKLY2FA_RESULT pass={0} fail={1}" -f (@($script:R).Count - $fails), $fails)
exit $fails
