' Silent launcher for ibkr-live-login.ps1 (window style 0 -- this machine's rule: no flashing
' consoles; -WindowStyle Hidden alone still flashes). The script itself shows message boxes.
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""D:\quant\scripts\windows\ibkr-live-login.ps1""", 0, False
