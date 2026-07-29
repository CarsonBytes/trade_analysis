# Run this once (interactively, as yourself) to make the quant dashboard watchdog start
# automatically at every future logon. Not run automatically by anything -- a deliberate,
# separate step from writing the watchdog script itself. Same pattern as this project's own
# Cloudflare Tunnel watchdog and event-radar's backend watchdog.
#
# To undo: delete the shortcut this creates, i.e.
#   Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\QuantDashboardWatchdog.vbs"

$startupDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
$dest = Join-Path $startupDir "QuantDashboardWatchdog.vbs"

Copy-Item -Path "D:\quant\watchdog-start.vbs" -Destination $dest -Force
Write-Host "Installed: $dest"
Write-Host "The watchdog will start automatically at your next logon."
Write-Host "To start it right now without logging out, run:"
Write-Host "  wscript.exe `"$dest`""
