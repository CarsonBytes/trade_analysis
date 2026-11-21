# Shared helpers for the LIVE gateway scripts (IBKR_WEEKLY_2FA_SPEC, 2026-11-21).
# Dot-source it:  . 'D:\quant\scripts\windows\ibkr-common.ps1'
#
# CLOCK RULE: everything here is HKT (UTC+8, no DST), computed from UTC. It NEVER uses the
# Windows local clock. The box's timezone was changed 4 times by tzutil between Nov 4 and
# Nov 21 (HKT -> EST -> HKT -> PST -> EST). Every local-clock rule moved with it: IBC's cold
# restart fired Monday 12:30 HKT, and the observe/verify scripts misdetected their phase.
# The user decided (2026-11-21): display and logs stay in HKT.
#
# Tests inject a clock with $global:IbkrNowOverride (a [datetime] in HKT).

$script:IbkrLogDir   = 'C:\IBC-Live\Logs'
$script:IbcLogFile   = 'C:\IBC-Live\Logs\IBC-3.24.0_GATEWAY-1047_.txt'
$script:LauncherLog  = 'C:\Jts-Live\launcher.log'
$script:LedgerFile   = 'C:\IBC-Live\Logs\touch-ledger.jsonl'
$script:LaunchFile   = 'C:\IBC-Live\Logs\ibc-launches.txt'
$script:WeeklyCfg    = 'C:\IBC-Live\weekly-touch.json'
$script:WeeklyState  = 'C:\IBC-Live\Logs\weekly-touch.state.json'
$script:DownState    = 'C:\IBC-Live\Logs\gateway-down.state.json'

# ---------------------------------------------------------------- clock + logging
function Get-HktNow {
    if ($global:IbkrNowOverride) { return [datetime]$global:IbkrNowOverride }
    return [DateTime]::SpecifyKind([DateTime]::UtcNow.AddHours(8), 'Unspecified')
}
function ConvertTo-Hkt([datetime]$dt) {
    # Any [datetime] carrying Kind Local/Utc -> HKT wall clock.
    return [DateTime]::SpecifyKind($dt.ToUniversalTime().AddHours(8), 'Unspecified')
}
function Format-Hkt([datetime]$dt) { return $dt.ToString('yyyy-MM-dd HH:mm:ss') + ' HKT' }
function Write-IbkrLog([string]$File, [string]$Msg) {
    try { ('{0} {1}' -f (Format-Hkt (Get-HktNow)), $Msg) | Add-Content -Path $File -Encoding UTF8 } catch { }
}

# ---------------------------------------------------------------- alerts
function Send-GatewayAlert([string]$Level, [string]$Key, [string]$Msg) {
    # Same channel as watchdog.ps1: gateway-push.sh <level> <key> <msg>. Best effort.
    if ($global:IbkrNoPush) { return }
    try {
        $m = "$Msg".Replace('\', '/')
        & wsl.exe -d Ubuntu -- bash /home/cap/quant/scripts/gateway-push.sh "$Level" "$Key" "$m" 2>$null | Out-Null
    } catch { }
}
function Show-IbkrToast([string]$Title, [string]$Text) {
    if ($global:IbkrNoPush) { return }
    try {
        Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', 'D:\quant\scripts\windows\toast.ps1',
            '-Title', ('"{0}"' -f $Title), '-Text', ('"{0}"' -f $Text)) | Out-Null
    } catch { }
}

# ---------------------------------------------------------------- IBC process + logs
function Get-IbcLiveProcs {
    Get-CimInstance Win32_Process -Filter "Name='java.exe' OR Name='cmd.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'IBC-Live' }
}
function Stop-IbcLive {
    # Kill the whole cmd tree: IBC's own restart loop relaunches the gateway if only java dies.
    foreach ($p in (Get-IbcLiveProcs | Where-Object { $_.Name -eq 'cmd.exe' })) { taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null }
    foreach ($p in (Get-IbcLiveProcs)) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
}
function Archive-IbcLogs([string]$Tag) {
    # IBC's log and launcher.log are OVERWRITTEN at every launch. Call this RIGHT BEFORE a
    # launch (not at script start): the Nov 20 session died during a 2.5h modal wait and its
    # log was overwritten unarchived. Folder names are HKT. Dedup: <120s since the last one.
    try {
        $root = Join-Path $script:IbkrLogDir 'archived'
        $newest = Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
            Sort-Object CreationTimeUtc -Descending | Select-Object -First 1
        if ($newest -and ([DateTime]::UtcNow - $newest.CreationTimeUtc).TotalSeconds -lt 120) { return $newest.FullName }
        $dest = Join-Path $root ('{0}_{1}' -f (Get-HktNow).ToString('yyyyMMdd_HHmmss'), $Tag)
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        Copy-Item $script:IbcLogFile -Destination $dest -Force -ErrorAction SilentlyContinue
        Copy-Item $script:LauncherLog -Destination $dest -Force -ErrorAction SilentlyContinue
        return $dest
    } catch { return $null }
}

# Parse "yyyy-MM-dd HH:mm:ss:fff" (IBC) or "yyyy-MM-dd HH:mm:ss.fff" (launcher). IBC's
# post-start lines are written in the gateway's clock (Asia/Shanghai = HKT, jts.ini); since
# 2026-11-21 the JVM is also pinned with -Duser.timezone=Asia/Hong_Kong (ibgateway.vmoptions).
function ConvertFrom-IbcStamp([string]$Line) {
    if ($Line -match '^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)') {
        return [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)
    }
    return $null
}

# Classify the CURRENT IBC-Live instance for the unattended login decision.
#   none              no IBC-Live process
#   prompt-pending    a 'Second Factor Authentication' window is open -> adopt it
#   starting          java started < 5 min ago, no auth outcome yet -> wait, do NOT kill
#   token-rejected    IBKR refused the saved token ("security tokens ... expired") -> relogin now
#   transient         "Server disconnected, please try again" -> relogin now (token is gone)
#   logged-out        2FA timed out / IBKR "Re-login is required" / LOGGED_OUT -> relogin now
#   unknown           anything else
# -IbcLines/-LauncherLines/-JavaAgeSec/-PromptOpen let tests feed archived logs.
function Get-IbcState {
    param([string[]]$IbcLines, [string[]]$LauncherLines, [double]$JavaAgeSec = -1, [int]$PromptOpen = -1, [int]$ProcCount = -1)
    if ($ProcCount -lt 0) { $ProcCount = @(Get-IbcLiveProcs).Count }
    if ($ProcCount -eq 0) { return 'none' }
    if ($PromptOpen -lt 0) {
        $PromptOpen = 0
        try { if ([IbkrWin]::Find('Second Factor Authentication') -ne [IntPtr]::Zero) { $PromptOpen = 1 } } catch { }
    }
    if ($PromptOpen -eq 1) { return 'prompt-pending' }
    if ($null -eq $IbcLines) { $IbcLines = @(Get-Content $script:IbcLogFile -Tail 400 -ErrorAction SilentlyContinue) }
    if ($null -eq $LauncherLines) { $LauncherLines = @(Get-Content $script:LauncherLog -Tail 1500 -ErrorAction SilentlyContinue) }
    # launcher.log is rewritten per JVM start, so its whole tail belongs to the current JVM.
    $lastStart = -1
    for ($i = 0; $i -lt $LauncherLines.Count; $i++) { if ($LauncherLines[$i] -match 'NS_AUTH_START') { $lastStart = $i } }
    if ($lastStart -ge 0) {
        for ($i = $lastStart; $i -lt $LauncherLines.Count; $i++) {
            if ($LauncherLines[$i] -match 'Authorization failed: .*security tokens') { return 'token-rejected' }
            if ($LauncherLines[$i] -match 'Authorization failed: Server disconnected') { return 'transient' }
        }
    }
    # IBC log: look at the latest session (after the last Starting Gateway / Re-starting session)
    $s = 0
    for ($i = 0; $i -lt $IbcLines.Count; $i++) { if ($IbcLines[$i] -match 'IBC: Starting Gateway|IBC: Re-starting session') { $s = $i } }
    $tail = if ($IbcLines.Count) { $IbcLines[$s..($IbcLines.Count - 1)] } else { @() }
    $lastLogin = -1; $lastOut = -1
    for ($i = 0; $i -lt $tail.Count; $i++) {
        $l = $tail[$i]
        if ($l -match 'IBC: Login has completed' -and $l -notmatch 'exiting via') { $lastLogin = $i }
        if ($l -match 'Re-login after second factor authentication timeout|Re-login is required|LoginState is LOGGED_OUT|Too many failed login attempts') { $lastOut = $i }
    }
    if ($lastOut -gt $lastLogin) {
        # A fresh JVM always passes through LOGGED_OUT before authenticating; it is only
        # "logged out" for real once it is older than the start-up window.
        if ($JavaAgeSec -lt 0) { $JavaAgeSec = Get-IbcJavaAgeSec }
        if ($JavaAgeSec -ge 0 -and $JavaAgeSec -lt 300 -and ($tail -join "`n") -notmatch 'Re-login after second factor|Too many failed|Re-login is required') { return 'starting' }
        return 'logged-out'
    }
    if ($JavaAgeSec -lt 0) { $JavaAgeSec = Get-IbcJavaAgeSec }
    if ($JavaAgeSec -ge 0 -and $JavaAgeSec -lt 300 -and $lastLogin -lt 0) { return 'starting' }
    return 'unknown'
}
function Get-IbcJavaAgeSec {
    $j = Get-CimInstance Win32_Process -Filter "Name='java.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'IBC-Live' } | Sort-Object CreationDate -Descending | Select-Object -First 1
    if (-not $j) { return -1 }
    return ([DateTime]::UtcNow - $j.CreationDate.ToUniversalTime()).TotalSeconds
}
# Why did the gateway go down? Recorded by the watchdog at the moment it notices, so the
# ledger can name the trigger of the full auth that follows.
function Get-IbcDownCause {
    $st = Get-IbcState
    switch ($st) {
        'none' {
            $tail = @(Get-Content $script:IbcLogFile -Tail 60 -ErrorAction SilentlyContinue) -join "`n"
            if ($tail -match 'IBKR Gateway; event=Closing') { return 'gateway-closed' }
            return 'no-process'
        }
        'logged-out' {
            $tail = @(Get-Content $script:IbcLogFile -Tail 400 -ErrorAction SilentlyContinue) -join "`n"
            if ($tail -match 'Re-login is required') { return 'ibkr-relogin' }
            return 'logged-out'
        }
        default { return $st }
    }
}

# ---------------------------------------------------------------- login lock + throttle
function Enter-LoginLock {
    # ONE login run per box: watchdog, autostart, weekly touch and the desktop shortcut all
    # take this. Nov 6 / Nov 19 / Nov 21: autostart + watchdog logins overlapped.
    $m = New-Object System.Threading.Mutex($false, 'Local\IBKRLiveLogin')
    try { if ($m.WaitOne(0)) { return $m } } catch [System.Threading.AbandonedMutexException] { return $m }
    return $null
}
function Test-LaunchBudget([int]$Max = 3, [int]$WindowMin = 30) {
    # Nov 19: 7 IBC launches in 22 min -> IBKR "Too many failed login attempts".
    $cut = [DateTime]::UtcNow.AddMinutes(-$WindowMin)
    $n = @(Get-Content $script:LaunchFile -ErrorAction SilentlyContinue | Where-Object {
        try { [DateTime]::Parse($_.Split(' ')[0], $null, 'RoundtripKind').ToUniversalTime() -gt $cut } catch { $false } }).Count
    return ($n -lt $Max)
}
function Register-IbcLaunch([string]$Who) {
    try { ('{0} {1} {2}' -f [DateTime]::UtcNow.ToString('o'), (Format-Hkt (Get-HktNow)).Replace(' ', 'T'), $Who) |
            Add-Content -Path $script:LaunchFile -Encoding UTF8 } catch { }
}

# ---------------------------------------------------------------- weekly window (HKT)
function Get-WeeklyConfig {
    $c = [ordered]@{ touchDay = 'Sunday'; touchTime = '20:35'; reminderMin = 10
        holdStartDay = 'Saturday'; holdStartTime = '06:00'; boundaryTime = '14:00'
        reportDay = 'Monday'; reportTime = '09:00' }
    if (Test-Path $script:WeeklyCfg) {
        try {
            $j = Get-Content $script:WeeklyCfg -Raw | ConvertFrom-Json
            foreach ($k in @($c.Keys)) { if ($null -ne $j.$k -and "$($j.$k)" -ne '') { $c[$k] = $j.$k } }
        } catch { }
    }
    return $c
}
function Get-DayInWeek([datetime]$Now, [string]$Day, [string]$Time) {
    # Weeks run Monday 00:00 -> Sunday 24:00 HKT. Returns $Day/$Time inside the week of $Now.
    $dow = [int]$Now.DayOfWeek; if ($dow -eq 0) { $dow = 7 }          # Mon=1..Sun=7
    $mon = $Now.Date.AddDays(1 - $dow)
    $t = [int][DayOfWeek]$Day; if ($t -eq 0) { $t = 7 }
    $hm = $Time.Split(':')
    return $mon.AddDays($t - 1).AddHours([int]$hm[0]).AddMinutes([int]$hm[1])
}
function Get-WeeklyWindow([datetime]$Now = (Get-HktNow)) {
    $c = Get-WeeklyConfig
    $touch = Get-DayInWeek $Now $c.touchDay $c.touchTime
    $hold = Get-DayInWeek $Now $c.holdStartDay $c.holdStartTime
    $boundary = Get-DayInWeek $Now $c.touchDay $c.boundaryTime
    $report = Get-DayInWeek $Now $c.reportDay $c.reportTime
    # The IBKR boundary that governs $Now: this week's Sunday 14:00 if passed, else last week's.
    $govBoundary = if ($Now -ge $boundary) { $boundary } else { $boundary.AddDays(-7) }
    return [pscustomobject]@{
        Now = $Now; Touch = $touch; Reminder = $touch.AddMinutes(-[int]$c.reminderMin)
        HoldStart = $hold; Boundary = $boundary; GoverningBoundary = $govBoundary; Report = $report
        WeekKey = $touch.ToString('yyyy-MM-dd')
        InHold = ($Now -ge $hold -and $Now -lt $touch)
    }
}
function Read-WeeklyState {
    if (Test-Path $script:WeeklyState) { try { return (Get-Content $script:WeeklyState -Raw | ConvertFrom-Json) } catch { } }
    return [pscustomobject]@{ reminderWeek = ''; touchWeek = ''; touchLaunchedAt = ''; reportWeek = '' }
}
function Save-WeeklyState($State) { $State | ConvertTo-Json | Set-Content -Path $script:WeeklyState -Encoding UTF8 }
# Which weekly phase is due now (or ''), for the watchdog to dispatch. Catch-up semantics:
# a phase stays due until done, within a bounded window (machine off at the exact minute).
function Get-DueWeeklyPhase([datetime]$Now = (Get-HktNow), $State = (Read-WeeklyState)) {
    $w = Get-WeeklyWindow $Now
    if ($Now -ge $w.Report -and $Now -lt $w.Report.AddHours(12) -and $State.reportWeek -ne $w.Report.ToString('yyyy-MM-dd')) { return 'report' }
    if ($Now -ge $w.Touch -and $Now -lt $w.Touch.AddHours(3) -and $State.touchWeek -ne $w.WeekKey) { return 'touch' }
    if ($Now -ge $w.Reminder -and $Now -lt $w.Touch -and $State.reminderWeek -ne $w.WeekKey) { return 'reminder' }
    return ''
}

# ---------------------------------------------------------------- touch ledger
function Read-TouchLedger {
    @(Get-Content $script:LedgerFile -ErrorAction SilentlyContinue | ForEach-Object { try { $_ | ConvertFrom-Json } catch { } })
}
function Add-LedgerEntry($Entry) {
    ($Entry | ConvertTo-Json -Compress) | Add-Content -Path $script:LedgerFile -Encoding UTF8
}
# Full auths = IBC 'Second Factor Authentication initiated' followed by 'Login has completed'
# (no 'Starting Gateway' in between). Token logins = 'Login has completed' without it.
function Get-IbcAuthEvents([string[]]$Lines) {
    $out = @(); $pending = $false
    foreach ($l in $Lines) {
        if ($l -match 'IBC: Starting Gateway|IBC: Re-starting session') { $pending = $false; continue }
        if ($l -match 'IBC: Second Factor Authentication initiated') { $pending = $true; continue }
        if ($l -match 'IBC: Login has completed' -and $l -notmatch 'exiting via') {
            $ts = ConvertFrom-IbcStamp $l
            if ($ts) { $out += [pscustomobject]@{ Kind = $(if ($pending) { 'full-auth' } else { 'token-login' }); Ts = $ts } }
            $pending = $false
        }
    }
    return $out
}
function Get-WarmVerdicts([string[]]$Lines) {
    $out = @(); $cur = $null
    foreach ($l in $Lines) {
        if ($l -match 'NS_AUTH_START .*initialTokenType=TST') {
            if ($cur) { $out += $cur }
            $cur = [pscustomobject]@{ Ts = (ConvertFrom-IbcStamp $l); Verdict = 'accepted' }
        } elseif ($cur -and $l -match 'Authorization failed: .*security tokens') { $cur.Verdict = 'rejected'
        } elseif ($cur -and $l -match 'Authorization failed: Server disconnected') { $cur.Verdict = 'transient' }
    }
    if ($cur) { $out += $cur }
    return $out
}
function Get-FullAuthCause([datetime]$Ts) {
    # Ordered evidence; first match wins. All windows are relative to the login moment.
    $st = Read-WeeklyState
    if ($st.touchLaunchedAt) {
        try { $tl = [datetime]::Parse($st.touchLaunchedAt); if ($Ts -ge $tl -and ($Ts - $tl).TotalHours -lt 3) { return 'weekly-touch' } } catch { }
    }
    try {
        $boot = ConvertTo-Hkt ((Get-CimInstance Win32_OperatingSystem).LastBootUpTime)
        if ($Ts -ge $boot -and ($Ts - $boot).TotalMinutes -lt 90) {
            $p41 = Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-Kernel-Power'; Id = 41; StartTime = $boot.AddHours(-8).AddMinutes(-5).ToLocalTime() } -MaxEvents 1 -ErrorAction SilentlyContinue
            if ($p41) { return 'power-loss' }
            $wu = Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'User32'; Id = 1074; StartTime = $boot.AddHours(-8).AddHours(-1).ToLocalTime() } -MaxEvents 3 -ErrorAction SilentlyContinue |
                Where-Object { $_.Message -match 'MoUsoCoreWorker|MoNotificationUx|TrustedInstaller' }
            if ($wu) { return 'windows-update-reboot' }
            return 'reboot'
        }
    } catch { }
    try {
        $wake = Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-Power-Troubleshooter'; Id = 1; StartTime = $Ts.AddMinutes(-90).AddHours(-8).ToLocalTime() } -MaxEvents 1 -ErrorAction SilentlyContinue
        if ($wake) { return 'sleep-resume' }
    } catch { }
    if (Test-Path $script:DownState) {
        try {
            $d = Get-Content $script:DownState -Raw | ConvertFrom-Json
            $since = [datetime]::Parse($d.sinceHkt)
            if ($Ts -ge $since -and ($Ts - $since).TotalHours -lt 36) { return $d.cause }
        } catch { }
    }
    return 'unknown'
}
# Scan the current IBC/launcher logs and append any new full-auth / token-login / warm-restart
# verdict to the ledger. Idempotent (id = kind + timestamp). Returns the new entries.
function Update-TouchLedger([string[]]$IbcLines, [string[]]$LauncherLines) {
    if ($null -eq $IbcLines) { $IbcLines = @(Get-Content $script:IbcLogFile -ErrorAction SilentlyContinue) }
    if ($null -eq $LauncherLines) { $LauncherLines = @(Get-Content $script:LauncherLog -ErrorAction SilentlyContinue) }
    $have = @{}; $ledger = Read-TouchLedger
    foreach ($e in $ledger) { $have[$e.id] = $true }
    $new = @()
    foreach ($ev in (Get-IbcAuthEvents $IbcLines)) {
        $id = '{0}:{1}' -f $ev.Kind, $ev.Ts.ToString('yyyyMMddTHHmmss')
        if ($have[$id]) { continue }
        $e = [ordered]@{ id = $id; kind = $ev.Kind; hkt = (Format-Hkt $ev.Ts) }
        if ($ev.Kind -eq 'full-auth') {
            $w = Get-WeeklyWindow $ev.Ts
            $prior = @($ledger + $new | Where-Object { $_.kind -eq 'full-auth' -and [datetime]::ParseExact($_.hkt.Substring(0, 19), 'yyyy-MM-dd HH:mm:ss', $null) -ge $w.GoverningBoundary })
            $e.cause = Get-FullAuthCause $ev.Ts
            # The week's ONE required touch is the first full auth after IBKR's Sunday boundary,
            # whatever triggered it. Everything after that in the same week is unplanned.
            $e.planned = ($prior.Count -eq 0)
            $e.week = $w.GoverningBoundary.ToString('yyyy-MM-dd')
            $dow = [int]$ev.Ts.DayOfWeek; $hm = $ev.Ts.Hour * 60 + $ev.Ts.Minute
            $e.rth = ($dow -ge 1 -and $dow -le 5 -and $hm -ge 22 * 60 + 30) -or ($dow -ge 2 -and $dow -le 6 -and $hm -lt 5 * 60)
        }
        $new += [pscustomobject]$e
    }
    foreach ($v in (Get-WarmVerdicts $LauncherLines)) {
        if (-not $v.Ts) { continue }
        $id = 'warm:{0}' -f $v.Ts.ToString('yyyyMMddTHHmmss')
        if ($have[$id]) {
            # a 'accepted' verdict written early may need upgrading to rejected/transient
            continue
        }
        if ($v.Verdict -eq 'accepted' -and ((Get-HktNow) - $v.Ts).TotalMinutes -lt 3) { continue }   # too early to judge
        $new += [pscustomobject]([ordered]@{ id = $id; kind = 'warm-restart'; hkt = (Format-Hkt $v.Ts); verdict = $v.Verdict })
    }
    foreach ($e in $new) { Add-LedgerEntry $e }
    return $new
}
function Test-WeekSatisfied([datetime]$Now = (Get-HktNow)) {
    $w = Get-WeeklyWindow $Now
    $hit = Read-TouchLedger | Where-Object { $_.kind -eq 'full-auth' -and
        [datetime]::ParseExact($_.hkt.Substring(0, 19), 'yyyy-MM-dd HH:mm:ss', $null) -ge $w.GoverningBoundary }
    return [bool]$hit
}

# ---------------------------------------------------------------- window finder
if (-not ('IbkrWin' -as [type])) {
Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
public class IbkrWin { public delegate bool P(IntPtr h, IntPtr l);
 [DllImport("user32.dll")] public static extern bool EnumWindows(P p, IntPtr l);
 [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 public static IntPtr Find(string title){ IntPtr found=IntPtr.Zero; EnumWindows((h,l)=>{ if(IsWindowVisible(h)){var s=new StringBuilder(256); GetWindowText(h,s,256); if(s.ToString()==title){found=h; return false;}} return true;}, IntPtr.Zero); return found; } }
"@
}
