' Silent launcher for the elevated passkey helper (window style 0, this machine's no-flash rule).
' Started by the scheduled task "\IBKR\Passkey Helper" (Run with highest privileges).
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""D:\quant\scripts\windows\ibkr-passkey-helper.ps1""", 0, False
