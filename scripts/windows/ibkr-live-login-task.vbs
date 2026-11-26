' Hidden launcher used by Task Scheduler task \IBKR\Live Login (2026-11-26).
' ibkr-live-login.ps1 re-dispatches itself here when it finds it was started from inside a
' Claude Code session, so IBC is a child of Task Scheduler, not of the Claude app (a Claude app
' quit killed the LIVE gateway on Wed 2026-11-25 16:41 HKT). Unattended: no MessageBoxes.
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""D:\quant\scripts\windows\ibkr-live-login.ps1"" -Unattended -Reason task-dispatch", 0, False
