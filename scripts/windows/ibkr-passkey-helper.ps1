# ELEVATED helper for the IBKR live passkey login (spec GATEWAY_PASSKEY, 2026-10-13).
#
# Runs ONLY via the scheduled task "\IBKR\Passkey Helper" (Run with highest privileges, user Cap,
# interactive). Why elevated: the Windows Security passkey dialog (CredentialUIBroker) rejects
# synthetic clicks/keys from normal-integrity processes -- verified 2026-10-13, a click from the
# login script was silently ignored and the process could not even be stopped.
#
# What it does, in the dialog for THIS login attempt only:
#   1. cancels leftover Security dialogs from earlier aborted attempts (they block new ones)
#   2. "選擇金鑰 / Choose a key"  -> selects 安全性金鑰 / Security key
#   3. PIN prompt                 -> types the YubiKey PIN from Windows Credential Manager
#                                    (generic credential "IBKR-YubiKey-PIN", stored by the user)
#   4. stops. The physical TOUCH of the key is always the human's.
#
# Safety: the PIN is typed ONLY while the foreground window belongs to CredentialUIBroker, it is
# never logged or written anywhere, and the helper acts only on a dialog that appeared after the
# request file written by ibkr-live-login.ps1 (so it never fires on someone else's prompt).
$Log = 'C:\IBC-Live\Logs\passkey-helper.log'
$Req = 'C:\IBC-Live\Logs\passkey-helper.request'
function Log($m) { "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m | Add-Content -Path $Log -Encoding UTF8 }

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
public class PH {
 [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
 [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, UIntPtr e);
 [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct CREDENTIAL {
   public uint Flags; public uint Type; public string TargetName; public string Comment;
   public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten; public uint CredentialBlobSize;
   public IntPtr CredentialBlob; public uint Persist; public uint AttributeCount; public IntPtr Attributes;
   public string TargetAlias; public string UserName; }
 [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool CredRead(string target, uint type, uint flags, out IntPtr cred);
 [DllImport("advapi32.dll")] public static extern void CredFree(IntPtr cred);
 public static string ReadSecret(string target) {
   IntPtr p; if (!CredRead(target, 1, 0, out p)) return null;
   try { var c = (CREDENTIAL)Marshal.PtrToStructure(p, typeof(CREDENTIAL));
         if (c.CredentialBlobSize == 0) return null;
         return Marshal.PtrToStringUni(c.CredentialBlob, (int)c.CredentialBlobSize / 2); }
   finally { CredFree(p); } }
 public static uint ForegroundPid() { uint pid; GetWindowThreadProcessId(GetForegroundWindow(), out pid); return pid; }
 // UI Automation does NOT list this dialog among the desktop's children (measured 2026-10-13), but
 // plain Win32 enumeration does: top-level, class "Credential Dialog Xaml Host", owned by the broker.
 public delegate bool EnumProc(IntPtr h, IntPtr l);
 [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc p, IntPtr l);
 [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
 [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
 public static IntPtr DialogFor(uint want) { IntPtr f = IntPtr.Zero; EnumWindows((h,l) => { uint pid; GetWindowThreadProcessId(h, out pid); if (pid == want && IsWindowVisible(h)) { var c = new StringBuilder(128); GetClassName(h, c, 128); if (c.ToString() == "Credential Dialog Xaml Host") { f = h; return false; } } return true; }, IntPtr.Zero); return f; }
 public static void ClickRel(IntPtr h, double fx, double fy) { RECT r; GetWindowRect(h, out r); SetForegroundWindow(h); System.Threading.Thread.Sleep(250); Click(r.L + (int)((r.R - r.L) * fx), r.T + (int)((r.B - r.T) * fy)); }
 public static void Click(int x, int y) { SetCursorPos(x, y); System.Threading.Thread.Sleep(150); mouse_event(2,0,0,0,UIntPtr.Zero); mouse_event(4,0,0,0,UIntPtr.Zero); }
}
"@
[PH]::SetProcessDPIAware() | Out-Null
$A = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]

Log "=== helper started (elevated=$(([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)))"
if (-not (Test-Path $Req)) { Log "no request file -- refusing to act (only ibkr-live-login.ps1 may trigger this)"; exit 1 }
$reqTime = [datetime]::Parse((Get-Content $Req -Raw).Trim())
Remove-Item $Req -Force
if (((Get-Date) - $reqTime).TotalMinutes -gt 5) { Log "request is stale ($reqTime) -- refusing to act"; exit 1 }

function DialogOf($proc) { $A::RootElement.FindFirst($TS::Children, (New-Object System.Windows.Automation.PropertyCondition($A::ProcessIdProperty, $proc.Id))) }
function Rect($el) { $r = $el.Current.BoundingRectangle; ,@([int]$r.Left, [int]$r.Top, [int]$r.Width, [int]$r.Height) }
function ClickRel($el, $fx, $fy) { $l,$t,$w,$h = Rect $el; [PH]::Click($l + [int]($w*$fx), $t + [int]($h*$fy)) }
function FindByName($el, $rx) { $el.FindAll($TS::Descendants, [System.Windows.Automation.Condition]::TrueCondition) | Where-Object { $_.Current.Name -match $rx } | Select-Object -First 1 }
function Press($el) {
    try { $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); return 'invoke' } catch {}
    try { $el.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select(); return 'select' } catch {}
    $r = $el.Current.BoundingRectangle; [PH]::Click([int]($r.Left + $r.Width/2), [int]($r.Top + $r.Height/2)); return 'click'
}

# 1. leftovers from aborted attempts block new requests -- cancel them
# Measured 2026-10-13: these leftovers are ORPHANED (the chromium that opened them was killed), so
# they ignore every click -- even elevated ones -- and Windows will not show a new request's
# dialog while they exist. Ending the process is the only thing that works; it needs elevation
# (a normal-integrity Stop-Process gets "Access is denied").
foreach ($p in @(Get-Process CredentialUIBroker -ErrorAction SilentlyContinue | Where-Object { $_.StartTime -lt $reqTime.AddSeconds(-5) })) {
    try { Stop-Process -Id $p.Id -Force -ErrorAction Stop; Log "ended orphaned Security dialog pid=$($p.Id) from $($p.StartTime)" }
    catch {
        Log "could not end leftover pid=$($p.Id): $($_.Exception.Message) -- trying its Cancel button"
        $hw = [PH]::DialogFor([uint32]$p.Id); if ($hw -ne [IntPtr]::Zero) { [PH]::ClickRel($hw, 0.72, 0.875); Log "  cancel by position" }
    }
    Start-Sleep -Milliseconds 800
}

# 2. find THIS attempt's dialog (a CredentialUIBroker window that appeared after the request)
$hwnd = [IntPtr]::Zero; $t0 = Get-Date
while ($hwnd -eq [IntPtr]::Zero -and ((Get-Date) - $t0).TotalSeconds -lt 60) {
    foreach ($p in @(Get-Process CredentialUIBroker -ErrorAction SilentlyContinue | Where-Object { $_.StartTime -ge $reqTime.AddSeconds(-5) })) { $h = [PH]::DialogFor([uint32]$p.Id); if ($h -ne [IntPtr]::Zero) { $hwnd = $h; $dlgPid = $p.Id; break } }
    if ($hwnd -eq [IntPtr]::Zero) { Start-Sleep -Milliseconds 500 }
}
if ($hwnd -eq [IntPtr]::Zero) { Log "no Security dialog for this attempt within 60s -- nothing done"; exit 2 }
Log "found this attempt's Security dialog (pid=$dlgPid hwnd=$hwnd)"
Start-Sleep -Milliseconds 1000
$dlg = $null; try { $dlg = $A::FromHandle($hwnd) } catch {}

# 3. key chooser -> Security key ("安全性金鑰" row sits at ~23% x / ~65% y of the 456x328 dialog)
$sk = $null; if ($dlg) { $sk = FindByName $dlg '^(安全性金鑰|Security key)$' }
if ($sk) { Log "chooser: selected Security key via $(Press $sk)" }
else { [PH]::ClickRel($hwnd, 0.23, 0.655); Log "chooser: clicked the Security key row by position" }

# 4. PIN prompt -> type the PIN, but only into CredentialUIBroker
$pin = [PH]::ReadSecret('IBKR-YubiKey-PIN')
if (-not $pin) { Log "no 'IBKR-YubiKey-PIN' in Credential Manager -- leaving PIN entry to the human"; exit 3 }
# Measured 2026-10-13: choosing the key makes Windows open the PIN screen in a NEW CredentialUIBroker
# process (the chooser's process goes away). Follow the newest broker that appeared after the choice.
$chooserPid = $dlgPid; $tChoice = Get-Date; $hwnd = [IntPtr]::Zero
while ($hwnd -eq [IntPtr]::Zero -and ((Get-Date) - $tChoice).TotalSeconds -lt 15) {
    foreach ($p in @(Get-Process CredentialUIBroker -ErrorAction SilentlyContinue | Where-Object { $_.StartTime -ge $reqTime.AddSeconds(-5) } | Sort-Object StartTime -Descending)) {
        $h = [PH]::DialogFor([uint32]$p.Id); if ($h -ne [IntPtr]::Zero -and ($p.Id -ne $chooserPid -or ((Get-Date) - $tChoice).TotalSeconds -gt 6)) { $hwnd = $h; $dlgPid = $p.Id; break }
    }
    if ($hwnd -eq [IntPtr]::Zero) { Start-Sleep -Milliseconds 400 }
}
if ($hwnd -eq [IntPtr]::Zero) { $pin = $null; Log "no PIN screen appeared after the key choice -- not typing anything"; exit 5 }
Log "PIN screen found (pid=$dlgPid, chooser was pid=$chooserPid)"
Start-Sleep -Milliseconds 1200
# Click INTO the PIN box first. Measured from screenshots (2026-10-13): the box spans ~55-62% of the
# dialog height in both layouts (with and without the "要求者 / requested by" line), centred in x.
# Without this click the keystrokes reached the dialog but not the box (box stayed empty).
[PH]::ClickRel($hwnd, 0.50, 0.585); Start-Sleep -Milliseconds 400
if ([PH]::ForegroundPid() -ne [uint32]$dlgPid) { $pin = $null; Log "foreground is NOT the Security dialog -- refusing to type the PIN"; exit 4 }
$escaped = ($pin.ToCharArray() | ForEach-Object { if ('+^%~(){}[]'.Contains([string]$_)) { "{$_}" } else { [string]$_ } }) -join ''
[System.Windows.Forms.SendKeys]::SendWait($escaped); Start-Sleep -Milliseconds 200
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
$pin = $null; $escaped = $null
Log "PIN entered (value not logged) -- waiting for the human to TOUCH the key"
exit 0
