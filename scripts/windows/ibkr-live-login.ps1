# IBKR LIVE gateway login on Windows -- the passkey ceremony, automated down to the one step
# only a human can do: TOUCH the YubiKey (spec GATEWAY_PASSKEY 2026-10-13, reworked by
# IBKR_WEEKLY_2FA_SPEC 2026-11-21).
#
#   1. one login run per box (named mutex, shared with autostart / watchdog / weekly touch)
#   2. classify any existing IBC-Live instance (ibkr-common.ps1 Get-IbcState):
#        prompt-pending -> adopt it AND start the elevated helper (Oct 18: adopting without the
#                          helper left the chooser/PIN untouched for 18 min)
#        starting       -> a (token) restart is in flight: wait, never kill it
#        token-rejected / transient / logged-out / unknown -> clear it and start a fresh login.
#                          IBKR already demands a touch; the old OKCancel "abandoned session?"
#                          modal only added darkness (14h Oct 23, 13.6h Oct 27, 10.5h Nov 6).
#   3. YubiKey present? (hub = warning only)
#   4. launch budget: <= 3 IBC launches / 30 min (Nov 19: 7 in 22 min -> IBKR throttle)
#   5. archive IBC/launcher logs RIGHT BEFORE the launch, launch IBC-Live hidden, click
#      "Authenticate >", run the elevated Passkey Helper (key choice + PIN) -- human touches
#   6. success = real API handshake (or IBC log + port + no auth dialog); write the touch ledger
#
# -Unattended (watchdog, autostart, weekly-touch): NO MessageBox anywhere -- every terminal
# state is log + toast (+ page where a human is needed) and the process EXITS. A lingering
# MessageBox kept this process alive on Nov 21 and the watchdog, seeing "login in progress",
# stopped re-prompting. Without -Unattended (desktop shortcut) only DECISIONS may ask.
# All log lines are HKT. Log: C:\IBC-Live\Logs\live-login.log
param(
    [int]$TimeoutSec = 210,
    [bool]$KeepAlive = $true,
    [switch]$Unattended,
    [string]$Reason = 'manual'
)
$LogFile = 'C:\IBC-Live\Logs\live-login.log'
. 'D:\quant\scripts\windows\ibkr-common.ps1'
. 'D:\quant\scripts\windows\ib-api-handshake.ps1'
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices;
public class LiveGW {
 [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
 [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
 [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, UIntPtr e);
 [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
 public static void Click(IntPtr h, double fx, double fy){ RECT r; GetWindowRect(h,out r); SetForegroundWindow(h); System.Threading.Thread.Sleep(300); SetCursorPos(r.L+(int)((r.R-r.L)*fx), r.T+(int)((r.B-r.T)*fy)); mouse_event(2,0,0,0,UIntPtr.Zero); mouse_event(4,0,0,0,UIntPtr.Zero); } }
"@
[LiveGW]::SetProcessDPIAware() | Out-Null

function Log($m) { Write-IbkrLog $LogFile $m }
# Terminal report: toast always, never a modal (GATEWAY_AUTOLOGIN §4 silent completion).
function Done($code, $msg, [switch]$Page) {
    Log $msg
    Show-IbkrToast 'IBKR Live Login' $msg
    if ($Page) { Send-GatewayAlert 'critical' 'gateway:login' "LIVE login: $msg" }
    if ($script:lock) { try { $script:lock.ReleaseMutex() } catch { } }
    exit $code
}
# A DECISION that needs a human: modal only when a human started us; unattended -> default.
function Ask($text, [bool]$UnattendedAnswer) {
    if ($Unattended) { return $UnattendedAnswer }
    $r = [System.Windows.Forms.MessageBox]::Show($text, 'IBKR Live Login', 'OKCancel', 'Warning')
    return ($r -eq 'OK')
}
function PortOpen { [bool](Get-NetTCPConnection -LocalPort 4001 -State Listen -ErrorAction SilentlyContinue) }
function LoggedIn { Test-IbApiHandshake -HostName '127.0.0.1' -Port 4001 -TimeoutSec 6 }
function LoginLooksDone {
    if (-not (PortOpen)) { return $false }
    if ([IbkrWin]::Find('Second Factor Authentication') -ne [IntPtr]::Zero) { return $false }
    if ([IbkrWin]::Find('Passkey Authentication') -ne [IntPtr]::Zero) { return $false }
    $recent = Get-Content (Get-IbcLogPath) -Tail 80 -ErrorAction SilentlyContinue
    return [bool]($recent -match 'IBC: Login has completed$')
}
function Start-Helper {
    (Get-Date).ToString('o') | Set-Content -Path 'C:\IBC-Live\Logs\passkey-helper.request' -Encoding ASCII
    schtasks /run /tn '\IBKR\Passkey Helper' 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Log "elevated helper started -- it picks Security key and enters the PIN; the human TOUCHES the key" }
    else { Log "helper task not set up -- human must choose Security key, enter PIN, touch"; Remove-Item 'C:\IBC-Live\Logs\passkey-helper.request' -Force -ErrorAction SilentlyContinue }
}
# Wait for a login to land. Returns $true on success (and records the ledger).
function Wait-Login([int]$Sec, [string]$What) {
    $t0 = Get-Date
    while (((Get-Date) - $t0).TotalSeconds -lt $Sec) {
        if (LoggedIn -or (LoginLooksDone)) {
            $how = if (LoggedIn) { 'API handshake OK' } else { 'IBC log + port + no auth dialog' }
            Log "$how at $([int]((Get-Date) - $t0).TotalSeconds)s ($What) -- authenticated session confirmed"
            try { foreach ($e in (Update-TouchLedger)) { Log "ledger: $($e.kind) $($e.hkt) cause=$($e.cause) planned=$($e.planned)" } } catch { Log "ledger update threw: $_" }
            return $true
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

$script:lock = Enter-LoginLock
if (-not $script:lock) { Log "=== live login requested (reason=$Reason) -- REJECTED: another login run holds the lock"; exit 3 }
Log "=== live login requested (reason=$Reason, unattended=$([bool]$Unattended))"
if (LoggedIn -or (LoginLooksDone)) { Done 0 'already logged in (handshake OK or IBC confirms login) -- nothing to do' }

$state = Get-IbcState
Log "existing IBC-Live state: $state"
if ($state -eq 'starting') {
    Log "an IBC-Live (re)start is in flight -- waiting up to 300s, NOT killing it (a token restart can take 2.5 min)"
    if (Wait-Login 300 'in-flight restart') { Done 0 'LIVE gateway is logged in (in-flight restart completed)' }
    $state = Get-IbcState
    Log "after waiting, IBC-Live state: $state"
}
if ($state -eq 'prompt-pending') {
    Log "a passkey prompt is already pending -- adopting it (no second login) and starting the helper"
    Start-Helper
    if (Wait-Login $TimeoutSec 'adopted prompt') { Done 0 'LIVE gateway is logged in (adopted the pending prompt)' }
    Done 2 "no touch within ${TimeoutSec}s on the pending prompt -- left running so a late touch still completes"
}
if ($state -eq 'unknown' -and (PortOpen) -and $Unattended) {
    # Port up + IBC alive but neither the handshake nor the log proves a login: could be a
    # healthy session whose API probe failed. Killing it would COST a touch -- a human decides.
    Done 2 'IBC-Live is running with 4001 listening but login is unproven -- NOT killing it unattended; check the gateway window' -Page
}
if ($state -ne 'none') {
    $ok = Ask ("An IBC-Live session is running but NOT logged in (state: $state).`n`nClose it and start a fresh login? This costs one YubiKey touch.") $true
    if (-not $ok) { Done 1 'cancelled at the clear-session prompt' }
    Log "clearing IBC-Live (state $state) so a fresh login can start"
    Archive-IbcLogs 'cleared' | Out-Null
    Stop-IbcLive
    Start-Sleep -Seconds 3
    if (Get-IbcLiveProcs) { Done 1 'could not clear the old IBC-Live session -- refusing to start a second one' -Page }
    Log "old session cleared"
}

$helperReady = [bool](Get-ScheduledTask -TaskPath '\IBKR\' -TaskName 'Passkey Helper' -ErrorAction SilentlyContinue)
$stale = @(Get-Process CredentialUIBroker -ErrorAction SilentlyContinue)
if ($stale.Count -gt 0 -and -not $helperReady) {
    Log "found $($stale.Count) leftover Windows Security dialog(s) from earlier attempts"
    if (-not (Ask "There are $($stale.Count) leftover 'Windows Security' passkey window(s). Click 取消 (Cancel) on each first, then OK here." $true)) { Done 1 'cancelled at stale-dialog warning' }
}
$key = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -match '^USB\\VID_1050' } | Select-Object -First 1
if ($env:IBKR_TEST_KEY -eq '0') { $key = $null }
# No page here (RETRO_2FA_2026-11-24 F2): the watchdog / weekly-touch own the key-absent page
# (once per incident) and launch the login the moment the key appears. Nov 22-23 this line
# paged every hour for 24 h.
if (-not $key) { Done 1 'YubiKey not plugged in -- plug it into a port directly on the PC; no login attempted (the watchdog starts the login when the key appears)' }
$parent = (Get-PnpDeviceProperty -InstanceId $key.InstanceId -KeyName DEVPKEY_Device_Parent -ErrorAction SilentlyContinue).Data
if ($parent -and $parent -notmatch 'ROOT_HUB') {
    Log "YubiKey is behind a USB hub ($parent) -- it has never answered through the hub"
    if (-not (Ask 'The YubiKey is on a USB HUB (it never answered through it). Move it to a port on the PC, then click OK.' $true)) { Done 1 'cancelled at hub warning' }
}
# Watchdog re-prompts do not page (F3: the watchdog pages once per incident + checkpoints).
if (-not (Test-LaunchBudget)) { Done 4 'launch budget exhausted (3 IBC launches in 30 min) -- not launching, IBKR throttles repeated logins; will retry later' -Page:($Reason -ne 'watchdog') }

Archive-IbcLogs 'login' | Out-Null
Register-IbcLaunch "login:$Reason"
Log "launching IBC-Live (IBC fills the stored credentials itself)"
Start-Process wscript.exe -ArgumentList '"C:\IBC-Live\start_hidden.vbs"'
$t0 = Get-Date; $clicked = $false
while (((Get-Date) - $t0).TotalSeconds -lt $TimeoutSec) {
    if (LoggedIn -or ($clicked -and (LoginLooksDone))) {
        Log "login confirmed at $([int]((Get-Date) - $t0).TotalSeconds)s"
        try { foreach ($e in (Update-TouchLedger)) { Log "ledger: $($e.kind) $($e.hkt) cause=$($e.cause) planned=$($e.planned)" } } catch { Log "ledger update threw: $_" }
        Done 0 'LIVE gateway is logged in'
    }
    if (-not $clicked) {
        $h = [IbkrWin]::Find('Second Factor Authentication')
        if ($h -ne [IntPtr]::Zero) {
            Start-Sleep -Milliseconds 1200
            (Get-Date).ToString('o') | Set-Content -Path 'C:\IBC-Live\Logs\passkey-helper.request' -Encoding ASCII
            [LiveGW]::Click($h, 0.496, 0.734); $clicked = $true
            Log "clicked 'Authenticate >'"
            Start-Helper
        }
    }
    Start-Sleep -Seconds 2
}
$GraceSec = 120
Log "main window of ${TimeoutSec}s elapsed -- watching a further ${GraceSec}s (IBC has come up late before)"
if (Wait-Login $GraceSec 'grace window') { Done 0 'LIVE gateway is logged in (during the grace window)' }
if ($KeepAlive) {
    Done 2 "no login within $([int](($TimeoutSec + $GraceSec) / 60)) min -- IBC-Live LEFT RUNNING so a late touch still completes (no kill, no auto-retry)"
}
Stop-IbcLive
Done 2 'no login in time -- IBC-Live stopped (KeepAlive off)'
