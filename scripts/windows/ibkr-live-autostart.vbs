' Hidden launcher for ibkr-live-autostart.ps1 (mirrors ibkr-live-login.vbs).
' Runs the quiet logon auto-start with no console window.
CreateObject("Wscript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""D:\quant\scripts\windows\ibkr-live-autostart.ps1""", 0, False
