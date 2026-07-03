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

# Background monitor: (re)launch the LIVE IB Gateway via IBC whenever port 4001 is down --
# mirrors dashboard.ps1's paper-gateway watchdog, pointed at the live IBC instance.
# NOTE: a cold Gateway (re)start needs a 2FA phone approval -- AutoRestartTime=08:00 in
# C:\IBC-Live\config.ini keeps the DAILY cycle session-preserving (no prompt); only a real
# crash/reboot needs you to tap approve.
$mon = Start-Job -ScriptBlock {
    function Test-Port($p) {
        try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', $p); $c.Close(); return $true }
        catch { return $false }
    }
    while ($true) {
        if (-not (Test-Port 4001)) {
            Start-Process -FilePath 'wscript.exe' `
                -ArgumentList '//B','//Nologo','C:\IBC-Live\start_hidden.vbs' -WindowStyle Hidden
            Start-Sleep -Seconds 45
        }
        Start-Sleep -Seconds 30
    }
}

try {
    Set-Location "D:\quant"
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
