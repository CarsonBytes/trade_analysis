# ============================================================================
#  LIVE dashboard launcher — for a scheduled task "DashboardAppLive" (Interactive),
#  mirroring dashboard.ps1's pattern. Runs CONCURRENTLY with the paper instance
#  (DashboardApp, port 8080) -- separate process, port, IB gateway/account,
#  database, and Cloudflare hostname. Neither can interfere with the other.
#
#  SAFETY: this is the ONLY place IB_ALLOW_LIVE is set. The paper instance never
#  sees it. The ib_exec guard additionally requires the connected account to
#  EXACTLY equal IB_ACCOUNT on a live port -- a mis-set value refuses to trade
#  rather than hitting the wrong book. DASH_FIXED_MODE=live pins this process's
#  identity explicitly (independent of the old single-endpoint switch pointer).
#
#  PREREQUISITES:
#   - The live IB Gateway (C:\IBC-Live, port 4001) logged in (first run needs
#     your live password in C:\IBC-Live\config.ini + a 2FA phone approval).
#   - Fractional shares + US-Stocks + Forex permissions on the live account.
#   - Paper-test one real fractional bracket order before funding meaningfully.
#   - Cloudflare: quant-live.carsonng.com routed to http://localhost:8081 (see
#     ~/.cloudflared/config.yml). NOTE: live.quant.carsonng.com (a 3rd-level
#     subdomain) does NOT work -- no cert covers it (Cloudflare's automatic
#     wildcard only covers *.carsonng.com, one level). quant-live.carsonng.com
#     is a 2nd-level subdomain that fits the existing wildcard -- works immediately.
# ============================================================================

$mutex = New-Object System.Threading.Mutex($false, "Global\DashboardAppLiveMutex")
if (-not $mutex.WaitOne(0, $false)) { exit 0 }

$env:DASH_FIXED_MODE = "live"         # pins this process to LIVE regardless of any shared pointer
$env:BROKER          = "ib"
$env:UNIVERSE        = "etf"
$env:IB_HOST         = "127.0.0.1"
$env:IB_PORT         = "4001"          # LIVE gateway port (paper is 4002)
$env:IB_CLIENT_ID    = "21"            # distinct from the paper instance (7)
$env:IB_ACCOUNT      = "U12991898"     # LIVE account id
$env:IB_ALLOW_LIVE   = "1"             # THE real-money switch (paper instance never sets this)
$env:DASH_PORT       = "8081"          # LIVE UI on its own local port
$env:PAPER_URL       = "https://quant.carsonng.com"
$env:LIVE_URL        = "https://quant-live.carsonng.com"
# Cash automation on the live book (optional; comment out to manage cash manually at first):
$env:CASH_USD        = "1"
$env:CASH_SWEEP      = "1"
# Panic-MR dip sleeve. PHASE2_NAV_USD (default 64000 = ~500K HKD) previously gated this OFF
# at the account's real size. 2026-07-12: user-confirmed decision to remove the gate now
# rather than wait for the account to grow into the original threshold. Verified before
# changing it (not just asserted): the original $64k number's own justification used a
# sleeve-edge estimate (+1.5pp CAGR) that this session's corrected re-verification found to
# be understated 2.4x (real edge +3.62pp); IBKR commission drag at current equity (~0.03-
# 0.06% round-trip) is smaller than the 10bp already modeled; and mechanical position sizing
# (fractional shares, ~1.7-3.5 share entries on SPY/QQQ at current equity) isn't degenerate.
# Setting PHASE2_NAV_USD=0 removes the equity gate entirely (sleeve_active() only depends on
# SLEEVE_ENABLED from here on) rather than pinning to today's specific dollar figure, which
# would drift out of date and could re-gate the sleeve OFF on an ordinary dip. This does NOT
# force a trade -- it only means the sleeve's actual VIX-panic entry condition, whenever it
# next fires for real, will be allowed to place an order instead of being blocked. Re-check
# the sleeve's real trade history once it has fills on the 11-ticker scope (8 of 11 added
# 2026-07-09 -- DIA/IWM/HYG/EFA/EEM/VNQ/PFF/ASHR -- have zero live-confirmed history, same as
# the original 3).
$env:PHASE2_NAV_USD  = "0"
$env:SLEEVE_ENABLED  = "1"

# Background monitor: (re)launch the LIVE IB Gateway via IBC whenever port 4001 is down --
# mirrors dashboard.ps1's paper-gateway watchdog, pointed at the live IBC instance.
# NOTE: a cold Gateway (re)start needs a 2FA phone approval -- AutoRestartTime=08:00 in
# C:\IBC-Live\config.ini keeps the DAILY cycle session-preserving (no prompt); only a real
# crash/reboot needs you to tap approve.
#
# ALSO handles the "stuck alive, never authenticated" failure mode (2026-07-08/09 HANDOFF):
# a Gateway that times out mid-2FA (SecondFactorAuthenticationTimeout=180, and does NOT self-
# heal when it fires -- the process just sits at the stuck dialog forever) never exits, so plain
# Test-Port-down relaunching is a no-op against it (there's already a process; launching another
# doesn't help). If port 4001 is STILL down after $stuckThresholdMin (user-set to 2min, 2026-07-09
# -- deliberately SHORTER than IBC's own 180s timeout, since that timeout doesn't recover on its
# own anyway, so there's no benefit to waiting for it) while a live gateway process is confirmed
# alive, force-kill it before relaunching -- same kill logic app.py's Restart button uses on
# demand, now automatic; this re-issues a FRESH 2FA push each time (a new login attempt = a new
# push), so a missed/expired push gets retried rather than leaving the account stuck waiting on
# one that already lapsed. Applies equally whether the down-episode started organically or from
# a manual Restart-button click -- this loop polls continuously regardless of what caused "port
# down." Capped at $maxAutoKills (10, ~20min of retrying at this cadence) so a problem that ISN'T
# the stuck-2FA case (e.g. a real credential/config error) doesn't retry forever; after the cap
# it falls back to passive relaunch-only (today's behavior) until manually fixed.
$mon = Start-Job -ScriptBlock {
    # GAME-EXPERIENCE GUARD (2026-07-12): the java.exe IB Gateway this job eventually spawns
    # (via wscript.exe -> cmd -> IBC's StartGateway.bat) was running at Normal priority,
    # competing with a foreground game for CPU. Setting priority AFTER the fact on an already-
    # running java.exe fails outright -- tried both .NET's `$proc.PriorityClass = ...`
    # ("Access is denied") and WMI's `Win32_Process.SetPriority()` (ReturnValue 5, also denied),
    # even from an elevated session -- this Gateway process's own security descriptor evidently
    # restricts PROCESS_SET_INFORMATION from outside callers (plausible hardening for financial
    # trading software; the SAME class of restriction this file's Kill-ProcessHard already works
    # around for PROCESS_TERMINATE, but that workaround (WMI) does NOT extend to SetPriority).
    # FIX: set priority on THIS job's OWN process instead (always allowed -- a process can set
    # its own priority) BEFORE spawning anything. Verified directly: Win32 CreateProcess defaults
    # a new child to NORMAL_PRIORITY_CLASS only when the CREATING process is itself Normal or
    # higher; when the parent is already BelowNormal/Idle, the child inherits that same lower
    # class by default. So every descendant this job spawns from here on (wscript -> cmd ->
    # StartGateway.bat -> java.exe) inherits BelowNormal for free, with no per-process access
    # rights needed at all. This is why the OTHER working process (`python -m dashboard.app`,
    # launched directly from the scheduled task's own already-BelowNormal PowerShell host, not
    # through this job) already showed BelowNormal correctly without any special-casing.
    try { (Get-Process -Id $PID).PriorityClass = 'BelowNormal' } catch { }
    function Test-Port($p) {
        try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', $p); $c.Close(); return $true }
        catch { return $false }
    }
    # Stop-Process -Force silently fails ("Access is denied") against this Gateway process --
    # it runs at a higher integrity/token level than this job's context, and -ErrorAction
    # SilentlyContinue swallowed the failure, so every "auto-kill" was a no-op that just left
    # the stuck process alive AND spawned a fresh duplicate on top. WMI's Win32_Process.
    # Terminate() uses a different privilege path and empirically works where Stop-Process
    # doesn't -- use that instead.
    function Kill-ProcessHard($procId) {
        try {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction Stop
            if ($p) { Invoke-CimMethod -InputObject $p -MethodName Terminate -ErrorAction Stop | Out-Null }
        } catch { }
    }
    # Find the live gateway java process by its COMMAND LINE (contains "IBC-Live"), not its
    # window title. The title changes throughout login (Login dialog -> "Authenticating..." ->
    # "Second Factor Authentication" -> only eventually "IBKR Gateway" once fully connected) --
    # matching on title alone made a process stuck mid-login completely invisible to this
    # check (found live, 2026-07-09: a process sat stuck at "Authenticating..." for 10+ min,
    # untouched by 5 straight "auto-kill" cycles because none of them ever saw it). The command
    # line's config path is static for the process's whole lifetime, so this can't miss a stuck
    # state the way the title match could.
    function Get-LiveGatewayProcs {
        Get-CimInstance Win32_Process -Filter "Name='java.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'IBC-Live' }
    }
    $stuckSince = $null
    $autoKillCount = 0
    $stuckThresholdMin = 2
    $maxAutoKills = 10
    while ($true) {
        if (Test-Port 4001) {
            $stuckSince = $null
            $autoKillCount = 0
        } else {
            if (-not $stuckSince) { $stuckSince = Get-Date }
            $downMin = ((Get-Date) - $stuckSince).TotalMinutes
            $gwProcs = Get-LiveGatewayProcs
            if ($gwProcs -and $downMin -ge $stuckThresholdMin -and $autoKillCount -lt $maxAutoKills) {
                Add-Content -Path 'C:\IBC-Live\watchdog.log' -Value (
                    "$(Get-Date -Format o) auto-kill: gateway alive $([math]::Round($downMin,1))min " +
                    "without port 4001 open (attempt $($autoKillCount + 1)/$maxAutoKills, pids " +
                    "$(($gwProcs | ForEach-Object { $_.ProcessId }) -join ','))")
                Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -ErrorAction SilentlyContinue |
                    Where-Object { $_.CommandLine -match 'StartGateway' } |
                    ForEach-Object { Kill-ProcessHard $_.ProcessId }
                $gwProcs | ForEach-Object { Kill-ProcessHard $_.ProcessId }
                Start-Sleep -Seconds 2
                $stillAlive = Get-LiveGatewayProcs
                Add-Content -Path 'C:\IBC-Live\watchdog.log' -Value (
                    "$(Get-Date -Format o) auto-kill result: " +
                    $(if ($stillAlive) {
                        "FAILED, still alive ($(($stillAlive | ForEach-Object { $_.ProcessId }) -join ','))"
                    } else { "confirmed dead" }))
                $autoKillCount++
                $stuckSince = Get-Date
            }
            Start-Process -FilePath 'wscript.exe' `
                -ArgumentList '//B','//Nologo','C:\IBC-Live\start_hidden.vbs' -WindowStyle Hidden
            Start-Sleep -Seconds 45
        }
        Start-Sleep -Seconds 30
    }
}

try {
    Set-Location "D:\quant"
    # Explicit, not just inherited from the scheduled task's own Priority=7 setting -- makes
    # this correct even if that task setting ever changes, and the child `python -m
    # dashboard.app` process below inherits it directly (confirmed: this is the process that
    # already showed BelowNormal correctly, unlike the gateway java.exe above).
    try { (Get-Process -Id $PID).PriorityClass = 'BelowNormal' } catch { }
    while ($true) {
        & .\.venv\Scripts\Activate.ps1
        python -m dashboard.app
        Start-Sleep -Seconds 10
    }
}
finally {
    if ($mon) { Stop-Job $mon -ErrorAction SilentlyContinue; Remove-Job $mon -Force -ErrorAction SilentlyContinue }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
