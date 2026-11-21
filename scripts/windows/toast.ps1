# Windows toast/balloon for the gateway watchdog (spec MIN-2FA, 2026-10-17). No modules --
# NotifyIcon balloon works on this box's PS 5.1 and shows in the notification area.
# Detached by watchdog.ps1 so the balloon's lifetime never blocks the 20s watchdog loop.
param(
    [Parameter(Mandatory)][string]$Title,
    [Parameter(Mandatory)][string]$Text
)
try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $ni = New-Object System.Windows.Forms.NotifyIcon
    $ni.Icon = [System.Drawing.SystemIcons]::Warning
    $ni.Visible = $true
    $ni.ShowBalloonTip(15000, $Title, $Text, [System.Windows.Forms.ToolTipIcon]::Warning)
    Start-Sleep -Seconds 16
    $ni.Dispose()
} catch { }
exit 0
