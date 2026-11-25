# One-time post-restart check (2026-10-14 ~20:35 HKT): did the 20:30 daily restart
# find the autorestart file (session token reused, NO passkey) or demand full auth?
# Appends findings to C:\IBC-Live\Logs\autorestart-check.log
$ErrorActionPreference = 'SilentlyContinue'
$CheckLog = 'C:\IBC-Live\Logs\autorestart-check.log'
function CLog($m) { "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m | Add-Content -Path $CheckLog -Encoding UTF8 }

CLog "=== post-20:30 restart check ==="
$ar = 'C:\Jts-Live\ajaninkbfndcbgmelgahgpfcmhmgggpefgeamgbo\autorestart'
if (Test-Path $ar) {
    $f = Get-Item $ar
    CLog "autorestart file PRESENT (length=$($f.Length), lastWrite=$($f.LastWriteTime))"
} else {
    CLog "autorestart file ABSENT -- next 20:30 restart will require full passkey"
}

# Grep the CURRENT IBC log for the autorestart decision (most recent lines win)
. 'D:\quant\scripts\windows\ibkr-common.ps1'
$ibcLog = Get-IbcLogPath   # version-derived (gateway 1047 -> 1051, 2026-11-25)
$lines = Select-String -Path $ibcLog -Pattern 'autorestart file|authentication will not be required|full authentication|IBC will autorestart|Cold restart' |
    Select-Object -Last 8
foreach ($l in $lines) { CLog "IBC: $($l.Line.Trim())" }

# Also check Jts-Live launcher.log for the autorestart read/delete cycle
$launcher = Select-String -Path 'C:\Jts-Live\launcher.log' -Pattern 'autorestart|AutoRestartDescriptor' |
    Select-Object -Last 6
foreach ($l in $launcher) { CLog "launcher: $($l.Line.Trim())" }

# Check if gateway API port is up (session survived the restart)
$up = Test-NetConnection -ComputerName 127.0.0.1 -Port 4001 -InformationLevel Quiet -WarningAction SilentlyContinue
CLog "API port 4001 reachable: $up"
CLog "=== end check ==="
