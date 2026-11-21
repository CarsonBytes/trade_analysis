# Restart observability for the LIVE IB Gateway (spec MIN-2FA, 2026-10-17).
#
# WHY: IBC's log (C:\IBC-Live\Logs\IBC-3.24.0_GATEWAY-1047_.txt) and C:\Jts-Live\launcher.log
# are OVERWRITTEN at every gateway launch, so the Oct 13/Oct 14 token-restart failures left no
# forensics. This task runs twice daily:
#   20:29 (PRE)  -- archive both logs BEFORE the scheduled 20:30 restart destroys them, and
#                   record pre-restart state (autorestart file existence/mtime ONLY -- never its
#                   bytes, it holds a session token).
#   20:34 (POST) -- record whether the restart was token-based (file "found"), whether a new IBC
#                   process is up, whether 4001 is listening, and what IBC's fresh log says
#                   (Login has completed / 2FA initiated / cold-restart schedule).
# Output: C:\IBC-Live\Logs\restart-observe.log  (append) + Logs\archived\<stamp>_<tag>\
#
# Task: \IBKR\Restart Observe (daily 20:29 + 20:34). Phase is auto-detected from the clock
# so the task can use a single action; -Phase allows manual runs.
param(
    [ValidateSet('auto', 'pre', 'post')]
    [string]$Phase = 'auto'
)
$ErrorActionPreference = 'SilentlyContinue'
$ObserveLog = 'C:\IBC-Live\Logs\restart-observe.log'
$IbcLog     = 'C:\IBC-Live\Logs\IBC-3.24.0_GATEWAY-1047_.txt'
$Launcher   = 'C:\Jts-Live\launcher.log'
$AutoRst    = 'C:\Jts-Live\ajaninkbfndcbgmelgahgpfcmhmgggpefgeamgbo\autorestart'
$ArchiveRoot = 'C:\IBC-Live\Logs\archived'
$StateFile  = 'C:\IBC-Live\Logs\restart-observe.state'

function Log($m) { "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m | Add-Content -Path $ObserveLog -Encoding UTF8 }

function Get-IbcPids {
    @(Get-CimInstance Win32_Process -Filter "Name='java.exe'" |
        Where-Object { $_.CommandLine -match 'IBC-Live' } |
        ForEach-Object { $_.ProcessId })
}
function Get-PortOwner($port, $addr = '*') {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $addr -eq '*' -or $_.LocalAddress -eq $addr } |
        Select-Object -First 1
    if (-not $c) { return $null }
    return "$($c.LocalAddress):$($c.OwningProcess)"
}
function Archive-IbcLogs($tag) {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $dest = Join-Path $ArchiveRoot "${stamp}_${tag}"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $copied = @()
    foreach ($f in @($IbcLog, $Launcher)) {
        if (Test-Path $f) { Copy-Item $f -Destination $dest -Force; $copied += (Split-Path $f -Leaf) }
    }
    return "$dest [$($copied -join ', ')]"
}
function Get-AutoRstState {
    if (Test-Path $AutoRst) {
        $i = Get-Item $AutoRst
        return "present mtime=$($i.LastWriteTime.ToString('HH:mm:ss')) size=$($i.Length)"
    }
    return 'absent'
}
function Get-SecurityWindow {
    # Any passkey/2FA prompt visible right now? (process main-window titles only)
    $w = Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -match 'Second Factor|Passkey|Security' } |
        ForEach-Object { $_.MainWindowTitle }
    if ($w) { return ($w -join ' | ') } else { return 'none' }
}

$RequestedPhase = $Phase
if ($Phase -eq 'auto') {
    $now = Get-Date
    $Phase = if ($now.Hour -eq 20 -and $now.Minute -le 31) { 'pre' } else { 'post' }
}
# START/END pair (MIN-2FA audit 2026-10-17): a missed run shows as an ABSENT START, a crash
# as START without END, and a delayed/late trigger is visible from the clock= vs 20:29/20:34.
Log ("START requested={0} detected={1} clock={2:HH:mm} ps={3} (auto: pre if 20:00-20:31 else post)" -f `
    $RequestedPhase, $Phase, (Get-Date), $PID)

if ($Phase -eq 'pre') {
    $arch = Archive-IbcLogs 'pre'
    $pids = Get-IbcPids
    Set-Content -Path $StateFile -Value ($pids -join ',') -Encoding UTF8
    Log ("PRE autorestart={0} ibcPids=[{1}] p4001={2} p4003v4={3} securityWin='{4}' archived={5}" -f `
        (Get-AutoRstState), ($pids -join ','), (Get-PortOwner 4001), (Get-PortOwner 4003 '0.0.0.0'), `
        (Get-SecurityWindow), $arch)
} else {
    $prev = (Get-Content $StateFile -ErrorAction SilentlyContinue) -join ','
    $pids = Get-IbcPids
    $newPid = if ($pids) { $pids -join ',' } else { 'NONE' }
    $finding = ''
    $login = 'no'
    $auth2fa = 'no'
    $cold = ''
    if (Test-Path $IbcLog) {
        # Read the WHOLE file (JVM banner alone is ~50 lines) and slice to the CURRENT
        # session: everything from the LAST 'autorestart file ...' line (each launch
        # appends its own launcher header) -- so a previous session's "Login has completed"
        # can never fake a healthy post-restart check.
        $all = @(Get-Content $IbcLog -ErrorAction SilentlyContinue)
        $idx = 0
        for ($i = $all.Count - 1; $i -ge 0; $i--) {
            if ($all[$i] -match 'autorestart file (found|not found)') { $idx = $i; break }
        }
        $cur = $all[$idx..($all.Count - 1)]
        if ($cur.Count -gt 0) {
            $finding = $cur[0].Trim()
            if ($cur -match 'Login has completed') { $login = 'yes' }
            if ($cur -match 'Second Factor Authentication initiated') { $auth2fa = 'yes' }
            $c = $cur | Select-String -Pattern 'Gateway will be cold restarted at' | Select-Object -First 1
            # Canonicalize to the bare "YYYY/MM/DD HH:MM": the log line is a full
            # sentence ("Gateway will be cold restarted at ..."), and
            # restart-verify.ps1's cold_schedule check compares against Get-ExpectedCold's
            # bare date -- storing the sentence made every real run FAIL cold_schedule
            # (2026-10-17 20:50 page). Extract the timestamp; fall back to the legacy
            # whole-line form if the line format ever changes again.
            if ($c) {
                if ($c.Line -match '(\d{4}/\d{2}/\d{2} \d{2}:\d{2})') { $cold = $Matches[1] }
                else { $cold = ($c.Line -replace '.*IBC:\s*', '').Trim() }
            }
        }
    }
    $restarted = if ($prev -and $newPid -ne 'NONE' -and $prev -ne $newPid) { 'yes' } elseif ($newPid -eq 'NONE') { 'NO-IBC' } else { 'same-or-unknown' }
    Log ("POST autorestart={0} ibcPid={1}(prev={2}) restarted={3} p4001={4} p4003v4={5} finding='{6}' loginCompleted={7} 2faInitiated={8} securityWin='{9}' cold='{10}'" -f `
        (Get-AutoRstState), $newPid, $prev, $restarted, (Get-PortOwner 4001), (Get-PortOwner 4003 '0.0.0.0'), `
        $finding, $login, $auth2fa, (Get-SecurityWindow), $cold)
}
Log ("END phase={0} ok=1" -f $Phase)
exit 0
