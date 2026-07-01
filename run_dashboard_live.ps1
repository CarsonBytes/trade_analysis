# ============================================================================
#  LIVE dashboard launcher — TEMPLATE (real money). Copy to C:\Scripts\ and fill in.
#  Runs a SECOND, ISOLATED dashboard instance for the LIVE account, concurrently
#  with the paper instance (which keeps using C:\Scripts\dashboard.ps1 on 8080).
#
#  SAFETY: this is the ONLY place IB_ALLOW_LIVE is set. The paper instance never
#  sees it, so a bug in one process cannot reach the other's account. The ib_exec
#  guard additionally requires the connected account to EXACTLY equal IB_ACCOUNT
#  on a live port — a mis-set value refuses to trade rather than hitting the wrong book.
#
#  PREREQUISITES (all manual, see HANDOFF.md go-live checklist):
#   - A SECOND IB Gateway logged into the LIVE account on port 4001 (separate IBC
#     instance / config; live username + password).
#   - Fractional shares + US-Stocks + Forex permissions on the live account.
#   - You have paper-tested one real fractional bracket order first.
# ============================================================================

$env:BROKER        = "ib"
$env:UNIVERSE      = "etf"
$env:IB_HOST       = "127.0.0.1"
$env:IB_PORT       = "4001"            # LIVE gateway port (paper is 4002)
$env:IB_CLIENT_ID  = "21"             # distinct from the paper instance (7)
$env:IB_ACCOUNT    = "U12991898"      # LIVE account id
$env:IB_ALLOW_LIVE = "1"             # <-- THE real-money switch (paper instance never sets this)
$env:DASH_PORT     = "8081"           # LIVE UI on a separate port (paper is 8080)
# Cash automation on the live book (optional; comment out to manage cash manually at first):
$env:CASH_USD      = "1"
$env:CASH_SWEEP    = "1"

Set-Location "D:\quant"               # or wherever the repo lives
# Watchdog loop: relaunch on exit (mirrors dashboard.ps1)
while ($true) {
    & .\.venv\Scripts\Activate.ps1
    python -m dashboard.app
    Start-Sleep -Seconds 5
}
