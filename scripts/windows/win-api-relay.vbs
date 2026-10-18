' Silent launcher for win-api-relay.ps1 (window style 0 -- this machine's rule: no flashing
' consoles; -WindowStyle Hidden alone still flashes). Run False = do not wait, so the relay
' powershell detaches from the task engine: a console-close event (the 0xC000013A class that
' killed the relay on 2026-10-16) can no longer reach it, and \IBKR\API Relay's action
' (wscript + this file) just spawns and completes. watchdog.ps1 restarts the task if the
' 0.0.0.0:4003 listener ever disappears. MISSING 2026-10-18 (task action pointed at a
' nonexistent file -> every engine start was a no-op phantom) -- recreated in the house
' launcher style of ibkr-live-login.vbs.
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""D:\quant\scripts\windows\win-api-relay.ps1""", 0, False
