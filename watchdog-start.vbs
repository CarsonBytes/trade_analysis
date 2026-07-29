' Launches the quant dashboard watchdog silently (no console window) at logon.
' Meant to be copied into the current user's Startup folder -- see watchdog.ps1 for
' what it actually does, and install-watchdog.ps1 for the (explicit, separate) install step.

Set objShell = CreateObject("WScript.Shell")
objShell.Run "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""D:\quant\watchdog.ps1""", 0, False
