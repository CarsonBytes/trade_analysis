' Silent launcher for the DashboardAppLive scheduled task (live dashboard, port 8081).
' wscript.exe + Run(...,0,False) avoids the console flash that -WindowStyle Hidden alone
' still causes when Task Scheduler calls powershell.exe directly.
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""D:\quant\run_dashboard_live.ps1""", 0, False
