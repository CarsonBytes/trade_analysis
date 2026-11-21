# Quiet auto-start of the LIVE IB Gateway at Windows logon (2026-10-14, reworked 2026-11-21
# by IBKR_WEEKLY_2FA_SPEC).
#
# After a reboot there is never an autorestart token (it is only written at a healthy
# scheduled restart), so this is ALWAYS a full passkey login: launch IBC-Live, auto-click
# "Authenticate >", trigger the elevated Passkey Helper (key choice + PIN), wait for the touch.
#
# WEEKEND HOLD (Sat 06:00 -> Sun weekly touch, HKT; weekly-touch.json): no markets are open
# and IBKR's Sunday reset invalidates any session made before it, so a login here would be a
# wasted touch (Nov 1 09:01/09:05, Nov 7 11:12). Inside the hold this script does NOT launch
# IBC; weekly-touch.ps1 prompts at the planned time. The desktop "IBKR Live Login" shortcut
# still works for a human who wants the gateway now.
#
# No MessageBoxes anywhere -- this runs hidden at every logon and must never block the desktop.
# Shares the login mutex with ibkr-live-login.ps1 (Nov 6/19/21: both ran at once).
# Log (HKT): C:\IBC-Live\Logs\live-login.log
$ErrorActionPreference = 'Stop'
$LogFile = 'C:\IBC-Live\Logs\live-login.log'
. 'D:\quant\scripts\windows\ibkr-common.ps1'
. 'D:\quant\scripts\windows\ib-api-handshake.ps1'
Add-Type @"
using System; using System.Runtime.InteropServices;
public class AutoGW {
 [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
 [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, UIntPtr e);
 [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
 public static void Click(IntPtr h, double fx, double fy){ RECT r; GetWindowRect(h,out r); SetForegroundWindow(h); System.Threading.Thread.Sleep(300); SetCursorPos(r.L+(int)((r.R-r.L)*fx), r.T+(int)((r.B-r.T)*fy)); mouse_event(2,0,0,0,UIntPtr.Zero); mouse_event(4,0,0,0,UIntPtr.Zero); } }
"@
function Log($m) { Write-IbkrLog $LogFile ("autostart: " + $m) }

$TimeoutSec = 300
$HandshakeOk = { Test-IbApiHandshake -HostName '127.0.0.1' -Port 4001 -TimeoutSec 6 }

Log "=== logon autostart requested"
if (& $HandshakeOk) { Log "already logged in (API handshake OK) -- nothing to do"; exit 0 }
$w = Get-WeeklyWindow
if ($w.InHold) {
    Log ("weekend hold until {0} -- NOT launching (a touch now is voided by IBKR's Sunday reset); weekly-touch prompts then, or use the 'IBKR Live Login' shortcut" -f (Format-Hkt $w.Touch))
    Show-IbkrToast 'IBKR gateway: weekend hold' ("Not logging in now. The weekly YubiKey touch is scheduled for {0}." -f (Format-Hkt $w.Touch))
    exit 0
}
$lock = Enter-LoginLock
if (-not $lock) { Log "another login run holds the lock -- leaving it to that run"; exit 3 }

if (-not (Get-IbcLiveProcs)) {
    if (-not (Test-LaunchBudget)) { Log "launch budget exhausted (3/30min) -- not launching"; exit 4 }
    Archive-IbcLogs 'autostart' | Out-Null
    Register-IbcLaunch 'autostart'
    Log "launching IBC-Live (hidden)"
    Start-Process wscript.exe -ArgumentList '"C:\IBC-Live\start_hidden.vbs"'
} else {
    Log "IBC-Live already running -- waiting, not launching a second instance"
}

$t0 = Get-Date; $clicked = $false
while (((Get-Date) - $t0).TotalSeconds -lt $TimeoutSec) {
    if (& $HandshakeOk) {
        Log "API handshake OK at $([int]((Get-Date) - $t0).TotalSeconds)s -- logged in$(if ($clicked) { ' (passkey touched)' } else { ' (no second factor asked)' })"
        try { foreach ($e in (Update-TouchLedger)) { Log "ledger: $($e.kind) $($e.hkt) cause=$($e.cause) planned=$($e.planned)" } } catch { }
        exit 0
    }
    if (-not $clicked) {
        $h = [IbkrWin]::Find('Second Factor Authentication')
        if ($h -ne [IntPtr]::Zero) {
            Start-Sleep -Milliseconds 1200
            (Get-Date).ToString('o') | Set-Content -Path 'C:\IBC-Live\Logs\passkey-helper.request' -Encoding ASCII
            [AutoGW]::Click($h, 0.496, 0.734); $clicked = $true
            Log "clicked 'Authenticate >' -- full passkey login required (no token survives a reboot)"
            schtasks /run /tn '\IBKR\Passkey Helper' 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { Log "elevated helper started -- TOUCH the YubiKey when the Security dialog shows" }
            else { Log "helper task not set up -- choose Security key, enter the PIN, touch"; Remove-Item 'C:\IBC-Live\Logs\passkey-helper.request' -Force -ErrorAction SilentlyContinue }
        }
    }
    Start-Sleep -Seconds 3
}
if ($clicked) { Log "no handshake after ${TimeoutSec}s -- prompt raised, IBC LEFT RUNNING so a late touch still completes" }
else { Log "no handshake and no passkey prompt after ${TimeoutSec}s -- IBC left running; the watchdog takes over" }
exit 2
