# Keepalive for D:\quant\watchdog.ps1 (ADDED 2026-11-21).
#
# Why: on Sat 2026-11-21 the watchdog process was hard-killed some time after 19:35 HKT. There
# was no log line, no PowerShell engine-stop event (403) and no crash record (1000/1026), which is
# what a TerminateProcess looks like. Nothing noticed for ~2 h. The same process dispatches the
# weekly IBKR touch (Sun 20:35) and supervises the dashboards, so a silent death is costly.
# Suspect (not proven): PUBG's anti-cheat (BattlEye BEDaisy.sys + navagio.sys) loaded at
# 19:30 HKT, 5 min before the last sign of life. Each restart below records whether
# TslGame / BEService are running, to confirm or rule that out.
#
# Run by task \IBKR\Quant Watchdog Keepalive every 5 min (setup-watchdog-keepalive.ps1) via
# ensure-quant-watchdog.vbs (hidden). Verdicts:
#   ok       a powershell running quant\watchdog.ps1 exists and its heartbeat is fresh
#   start    no such process            -> launch it via the Startup VBS
#   restart  process exists, heartbeat stale (hung) -> kill it, then launch
#   wait     machine booted < 4 min ago (the Startup-folder launch is still coming) -> do nothing
#
# Dot-source to unit-test Get-KeepaliveVerdict (scripts\windows\test-watchdog-keepalive.ps1).
param([switch]$DryRun)

$script:HeartbeatFile = 'D:\quant\logs\watchdog.heartbeat'
$script:SupLog        = 'D:\quant\logs\watchdog-keepalive.log'
$script:StateFile     = 'D:\quant\logs\watchdog-keepalive.state.json'
$script:Launcher      = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\QuantDashboardWatchdog.vbs'
$script:StaleSec      = 300     # 15 missed 20 s cycles
$script:BootGraceSec  = 240
$script:StormCount    = 3       # restarts within StormWindowMin -> one critical page per day
$script:StormWindowMin = 60

function Get-KeepaliveVerdict {
    param([int]$ProcCount, [Nullable[double]]$HeartbeatAgeSec, [double]$UptimeSec)
    if ($UptimeSec -lt $script:BootGraceSec -and $ProcCount -eq 0) { return 'wait' }
    # A fresh heartbeat is proof of life on its own. Process detection goes through WMI, which can
    # come back empty under load (seen 2026-11-21 22:10 HKT: "NOT RUNNING" while pid 25224 was
    # alive); a live heartbeat must win over that.
    if ($null -ne $HeartbeatAgeSec -and $HeartbeatAgeSec -le $script:StaleSec) { return 'ok' }
    if ($ProcCount -eq 0) { return 'start' }
    # Process present, heartbeat stale or missing. The caller caps the age at the process age, so
    # a just-started watchdog that hasn't written yet is not mistaken for a hung one.
    return 'restart'
}

function Write-SupLog([string]$msg) {
    $ts = [DateTime]::UtcNow.AddHours(8).ToString('yyyy-MM-dd HH:mm:ss')
    $line = "$ts HKT [pid $PID] $msg`r`n"
    for ($i = 0; $i -lt 3; $i++) {
        try { [IO.File]::AppendAllText($script:SupLog, $line, [Text.UTF8Encoding]::new($false)); return }
        catch { Start-Sleep -Milliseconds 300 }
    }
}

function Send-KeepaliveAlert([string]$level, [string]$msg) {
    try {
        & wsl.exe -d Ubuntu -- bash /home/cap/quant/scripts/gateway-push.sh $level 'watchdog:keepalive' $msg 2>$null | Out-Null
    } catch { }
}

function Get-WatchdogProcs {
    $found = @()
    try {
        $found = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -OperationTimeoutSec 20 -ErrorAction Stop |
            Where-Object { $_.CommandLine -match 'quant\\watchdog\.ps1' -and $_.ProcessId -ne $PID } |
            ForEach-Object { [pscustomobject]@{ ProcessId = [int]$_.ProcessId; CreationDate = $_.CreationDate } })
    } catch { }
    if ($found.Count -eq 0) {
        # WMI empty or failed: fall back to the pid the watchdog wrote itself, via Get-Process (no WMI).
        $pidFile = 'D:\quant\logs\watchdog.pid'
        $lp = "$(Get-Content $pidFile -ErrorAction SilentlyContinue)".Trim()
        if ($lp -match '^\d+$' -and [int]$lp -ne $PID) {
            $p = Get-Process -Id ([int]$lp) -ErrorAction SilentlyContinue
            # Pid reuse guard: the watchdog writes the pid file right after it starts, so the real
            # owner started before that write. A process that started later merely reused the number.
            if ($p -and $p.ProcessName -in 'powershell', 'pwsh' -and
                $p.StartTime -le (Get-Item $pidFile).LastWriteTime.AddSeconds(5)) {
                $found = @([pscustomobject]@{ ProcessId = $p.Id; CreationDate = $p.StartTime })
            }
        }
    }
    return $found
}

function Invoke-Keepalive {
    $procs = @(Get-WatchdogProcs)   # @(): PS 5.1 gives a single object no .Count
    $uptime = ((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalSeconds
    $hbAge = $null; $hbTxt = 'none'
    if (Test-Path $script:HeartbeatFile) {
        $hbAge = ([DateTime]::UtcNow - (Get-Item $script:HeartbeatFile).LastWriteTimeUtc).TotalSeconds
        $hbTxt = "{0}s ago" -f [int]$hbAge
    }
    if ($procs.Count -gt 0) {
        # Process age caps the heartbeat age: a watchdog that started 30 s ago and hasn't written
        # yet is not hung, whatever an old heartbeat file says.
        $oldest = ($procs | ForEach-Object { ([DateTime]::Now - $_.CreationDate).TotalSeconds } | Measure-Object -Minimum).Minimum
        if ($null -eq $hbAge -or $hbAge -gt $oldest) { $hbAge = $oldest }
    }
    $verdict = Get-KeepaliveVerdict -ProcCount $procs.Count -HeartbeatAgeSec $hbAge -UptimeSec $uptime
    if ($verdict -in 'ok', 'wait') { return $verdict }

    $game = @(Get-Process -Name TslGame, BEService, BEService_x64 -ErrorAction SilentlyContinue | ForEach-Object Name | Sort-Object -Unique) -join ','
    if (-not $game) { $game = 'none' }
    $lastHb = if (Test-Path $script:HeartbeatFile) { "$(Get-Content $script:HeartbeatFile -TotalCount 1)" } else { 'none' }
    $why = if ($verdict -eq 'start') { 'NOT RUNNING' } else { "HUNG (heartbeat $hbTxt)" }
    Write-SupLog "watchdog $why -- last heartbeat: $lastHb; game/anti-cheat running now: $game; uptime $([int]($uptime/60)) min"
    if ($DryRun) { Write-SupLog "dry-run: would $verdict"; return $verdict }

    if ($verdict -eq 'restart') {
        foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
    }
    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"{0}"' -f $script:Launcher) | Out-Null
    Start-Sleep -Seconds 25
    $ok = @(Get-WatchdogProcs).Count -gt 0 -and (Test-Path $script:HeartbeatFile) -and
          (([DateTime]::UtcNow - (Get-Item $script:HeartbeatFile).LastWriteTimeUtc).TotalSeconds -lt 60)
    Write-SupLog ("relaunched via Startup VBS -> " + $(if ($ok) { 'running, heartbeat fresh' } else { 'NOT confirmed yet' }))

    # restart accounting: one digest line per restart, one critical page per day on a storm
    $state = [pscustomobject]@{ restarts = @(); stormPagedDay = '' }
    if (Test-Path $script:StateFile) { try { $state = Get-Content $script:StateFile -Raw | ConvertFrom-Json } catch { } }
    $now = [DateTime]::UtcNow
    $recent = @(@($state.restarts) | Where-Object { $_ -and ($now - [DateTime]::Parse($_).ToUniversalTime()).TotalMinutes -lt $script:StormWindowMin })
    $recent += $now.ToString('o')
    $day = $now.AddHours(8).ToString('yyyy-MM-dd')
    $msg = "quant watchdog was $($why.ToLower()) (last heartbeat $lastHb; game: $game) -- relaunched" + $(if ($ok) { '' } else { ' but NOT confirmed' })
    if (-not $ok -or ($recent.Count -ge $script:StormCount -and $state.stormPagedDay -ne $day)) {
        Send-KeepaliveAlert 'critical' ("$msg; $($recent.Count) restart(s) in ${script:StormWindowMin} min")
        if ($recent.Count -ge $script:StormCount) { $state.stormPagedDay = $day }
    } else {
        Send-KeepaliveAlert 'error' $msg
    }
    [pscustomobject]@{ restarts = $recent; stormPagedDay = $state.stormPagedDay } | ConvertTo-Json | Set-Content $script:StateFile -Encoding UTF8
    return $verdict
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-Keepalive | Out-Null } catch { Write-SupLog "keepalive threw: $_" }
}
