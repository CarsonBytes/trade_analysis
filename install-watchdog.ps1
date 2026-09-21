# Run this once (interactively, as yourself) to make the quant dashboard watchdog start
# automatically at every future logon. Not run automatically by anything -- a deliberate,
# separate step from writing the watchdog script itself. Same pattern as this project's own
# Cloudflare Tunnel watchdog and event-radar's backend watchdog.
#
# What it supervises (repointed 2026-09-12): the WSL2/Docker dashboards on localhost:18080
# (paper) and :18081 (live), probed from the WINDOWS side. It complements infra-watchdog.sh
# rather than duplicating it -- that one runs inside WSL and so cannot see the failure class
# where WSL, Docker, or Windows localhost forwarding is itself the broken thing. See
# watchdog.ps1's header for the escalation ladder and why `wsl --shutdown` is not automated.
#
# To undo: delete the copy this creates, i.e.
#   Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\QuantDashboardWatchdog.vbs"
# and stop the running loop:
#   Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
#       Where-Object { $_.CommandLine -like '*D:\quant\watchdog.ps1*' } |
#       ForEach-Object { Stop-Process -Id $_.ProcessId }

$startupDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
$dest = Join-Path $startupDir "QuantDashboardWatchdog.vbs"

Copy-Item -Path "D:\quant\watchdog-start.vbs" -Destination $dest -Force
Write-Host "Installed: $dest"
Write-Host "The watchdog will start automatically at your next logon."
Write-Host "To start it right now without logging out, run:"
Write-Host "  wscript.exe `"$dest`""
