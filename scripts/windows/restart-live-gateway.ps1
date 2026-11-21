# Restart the LIVE IB Gateway in one step (2026-10-14).
#
# Kills the current IBC-Live session, waits for port 4001 to be released, then launches
# ibkr-live-login.ps1 which handles everything down to the YubiKey touch. No AI needed.
#
# Usage: right-click -> Run with PowerShell, or from a terminal:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\restart-live-gateway.ps1
#
# The login script opens its own window with the passkey prompt. Touch the key when the
# Windows Security dialog appears. On success the dashboard reconnects within ~30s.
$ErrorActionPreference = 'Stop'
$LogFile = 'C:\IBC-Live\Logs\live-login.log'
# HKT log stamps regardless of the Windows timezone (IBKR_WEEKLY_2FA_SPEC 2026-11-21).
. 'D:\quant\scripts\windows\ibkr-common.ps1'
function Log($m) { Write-IbkrLog $LogFile $m }

Log "=== restart-live-gateway requested"

# 0a. Single-instance guard: two runs 34s apart on 2026-10-14 launched two IBC
#     instances, two helpers, and "no Security dialog" timeouts. A named mutex
#     rejects any concurrent run outright.
$hasHandle = $false
$mutex = New-Object System.Threading.Mutex($false, 'Global\IBKRLiveRestart')
try { $hasHandle = $mutex.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $hasHandle = $true }  # prior run died -- safe to take
if (-not $hasHandle) {
    Log "REJECTED: another restart-live-gateway run is in progress -- nothing done"
    exit 1
}

# 0b. In-progress-login guard: if ibkr-live-login.ps1 (from this or the logon autostart)
#     is already running, do NOT kill mid-login; the existing window is the one to touch.
$loginProcs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'ibkr-live-(login|autostart)\.ps1' }
if ($loginProcs) {
    Log "REJECTED: a login is already in progress (pid $($loginProcs[0].ProcessId)) -- touch the YubiKey in the existing window instead"
    exit 1
}

# 1. Kill the whole IBC-Live cmd tree (IBC's restart loop relaunches the gateway if only java dies).
$procs = Get-CimInstance Win32_Process -Filter "Name='java.exe' OR Name='cmd.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'IBC-Live' }
if ($procs) {
    foreach ($p in ($procs | Where-Object { $_.Name -eq 'cmd.exe' })) {
        taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
    }
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Log "killed $($procs.Count) IBC-Live process(es)"
} else {
    Log "no IBC-Live process found -- nothing to kill"
}

# 2. Wait for port 4001 to be released (the gateway holds it until the process fully exits).
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    $listening = Get-NetTCPConnection -LocalPort 4001 -State Listen -ErrorAction SilentlyContinue
    if (-not $listening) { break }
    Start-Sleep -Milliseconds 500
}
if (Get-NetTCPConnection -LocalPort 4001 -State Listen -ErrorAction SilentlyContinue) {
    Log "WARNING: port 4001 still listening after 20s -- a process may be holding it"
} else {
    Log "port 4001 released"
}
Start-Sleep -Seconds 2

# 3. Launch the login script in a visible window (it handles guards, IBC launch, Authenticate
#    auto-click, Passkey Helper, and success detection). The human only touches the key.
Log "launching ibkr-live-login.ps1"
Start-Process powershell.exe -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass',
    '-File', 'D:\quant\scripts\windows\ibkr-live-login.ps1', '-Reason', 'manual-restart'
) -WorkingDirectory 'D:\quant'
Log "login script launched -- watch for the passkey prompt and touch the YubiKey"
