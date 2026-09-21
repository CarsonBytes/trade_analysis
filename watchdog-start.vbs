' Launches the quant dashboard watchdog silently (no console window, mode 0) at logon.
' Meant to be copied into the current user's Startup folder as QuantDashboardWatchdog.vbs --
' see install-watchdog.ps1 for the (explicit, separate) install step, and watchdog.ps1 for
' what it actually does.
'
' As of 2026-09-12 watchdog.ps1 supervises the WSL2/Docker dashboards (localhost:18080 and
' :18081) from the WINDOWS side, which is the only place the WSL-localhost-forwarding failure
' class is visible. It is NOT a replacement for infra-watchdog.sh inside WSL; it is the
' outside observer that one structurally cannot be.

Set objShell = CreateObject("WScript.Shell")
objShell.Run "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""D:\quant\watchdog.ps1""", 0, False
