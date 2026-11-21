# Tests for the watchdog keepalive (2026-11-21). Pure decision tests + static checks; never kills,
# never launches, never pages.  Run: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\test-watchdog-keepalive.ps1
$ErrorActionPreference = 'Stop'
$repo = Split-Path (Split-Path $PSScriptRoot)
. (Join-Path $PSScriptRoot 'ensure-quant-watchdog.ps1')
$pass = 0; $fail = 0
function Check($name, $got, $want) {
    if ("$got" -eq "$want") { "[PASS] $name"; $script:pass++ } else { "[FAIL] $name -- got '$got' want '$want'"; $script:fail++ }
}

'verdicts:'
Check 'running + fresh heartbeat -> ok'        (Get-KeepaliveVerdict -ProcCount 1 -HeartbeatAgeSec 15  -UptimeSec 9999) 'ok'
Check 'no process -> start'                    (Get-KeepaliveVerdict -ProcCount 0 -HeartbeatAgeSec 7000 -UptimeSec 9999) 'start'
Check 'no process, no heartbeat file -> start' (Get-KeepaliveVerdict -ProcCount 0 -HeartbeatAgeSec $null -UptimeSec 9999) 'start'
Check 'process, heartbeat 6 min old -> restart' (Get-KeepaliveVerdict -ProcCount 1 -HeartbeatAgeSec 360 -UptimeSec 9999) 'restart'
Check 'process, heartbeat exactly at limit -> ok' (Get-KeepaliveVerdict -ProcCount 1 -HeartbeatAgeSec 300 -UptimeSec 9999) 'ok'
Check 'just booted, nothing yet -> wait'       (Get-KeepaliveVerdict -ProcCount 0 -HeartbeatAgeSec $null -UptimeSec 60) 'wait'
Check 'just booted but running -> ok'          (Get-KeepaliveVerdict -ProcCount 1 -HeartbeatAgeSec 10 -UptimeSec 60) 'ok'
Check 'WMI saw no process but heartbeat fresh -> ok (22:10 false alarm)' (Get-KeepaliveVerdict -ProcCount 0 -HeartbeatAgeSec 20 -UptimeSec 9999) 'ok'
Check 'process with no heartbeat file -> restart' (Get-KeepaliveVerdict -ProcCount 1 -HeartbeatAgeSec $null -UptimeSec 9999) 'restart'

'static:'
$wd = Get-Content (Join-Path $repo 'watchdog.ps1') -Raw
Check 'watchdog writes the heartbeat each cycle' ([bool]($wd -match 'Set-Content -Path \$heartbeatFile')) 'True'
Check 'heartbeat path matches the keepalive' ([bool]($wd -match [regex]::Escape($script:HeartbeatFile))) 'True'
Check 'lock check verifies the command line (pid reuse)' ([bool]($wd -match "\`$cmd -match 'quant\\\\watchdog\\.ps1'")) 'True'
Check 'lock check has a start-time fallback when WMI fails' ([bool]($wd -match 'StartTime -le \(Get-Item \$lockFile\)')) 'True'
Check 'Startup launcher exists' (Test-Path $script:Launcher) 'True'
$ka = Get-Content (Join-Path $PSScriptRoot 'ensure-quant-watchdog.ps1') -Raw
# 2026-11-21: a bare `(Get-WatchdogProcs).Count` is $null for ONE process in PS 5.1, so a live
# watchdog read as "NOT RUNNING" and was needlessly relaunched twice. Every count must use @().
Check 'every Get-WatchdogProcs count is @()-wrapped' ([regex]::Matches($ka, '(?<!@)\(Get-WatchdogProcs\)\.Count').Count + [regex]::Matches($ka, '\$procs = Get-WatchdogProcs').Count) 0
$e = $null; $t = $null
[void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $repo 'watchdog.ps1'), [ref]$t, [ref]$e)
Check 'watchdog.ps1 parses' $e.Count 0
[void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'ensure-quant-watchdog.ps1'), [ref]$t, [ref]$e)
Check 'ensure-quant-watchdog.ps1 parses' $e.Count 0

'live (read-only):'
$task = Get-ScheduledTask -TaskPath '\IBKR\' -TaskName 'Quant Watchdog Keepalive' -ErrorAction SilentlyContinue
Check 'keepalive task registered and enabled' ([bool]($task -and $task.State -ne 'Disabled')) 'True'
Check 'exactly one watchdog process' (@(Get-WatchdogProcs).Count) 1
$hb = if (Test-Path $script:HeartbeatFile) { [int]([DateTime]::UtcNow - (Get-Item $script:HeartbeatFile).LastWriteTimeUtc).TotalSeconds } else { 99999 }
Check 'heartbeat fresh (< 60 s)' ($hb -lt 60) 'True'

""
"$pass passed, $fail failed"
if ($fail) { exit 1 }
