' Silent launcher for task \IBKR\Quant Watchdog Keepalive (window style 0 = no console flash).
' See ensure-quant-watchdog.ps1.
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""D:\quant\scripts\windows\ensure-quant-watchdog.ps1""", 0, True
