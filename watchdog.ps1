# Independent watchdog for BOTH quant dashboards (paper :8080, live :8081) -- runs
# indefinitely, checking every 20s, and calls Start-ScheduledTask whenever a port isn't
# actually answering. Same pattern already proven for this project's own Cloudflare Tunnel
# (see project-quant-remote-access memory) and for event-radar's backend
# (D:\event-radar\deploy\watchdog.ps1) -- ported here 2026-07-30 after a real incident: the
# LIVE dashboard was down ~80+ minutes undetected because the wrapper script
# (run_dashboard_live.ps1) had already exited "successfully" hours before the actual
# failure, so Task Scheduler's own restart-on-failure never fired (exit 0 isn't a failure),
# and the existing port-based orphan guard inside that script only runs at the START of a
# NEW invocation -- which nothing was triggering. This watchdog is that missing trigger,
# fully independent of Task Scheduler's own bookkeeping.
#
# NOT auto-installed -- this file alone changes nothing. See install-watchdog.ps1 (or just
# copy watchdog-start.vbs into your Startup folder) for the actual install step.

$logFile = "D:\quant\logs\watchdog.log"
$lockFile = "D:\quant\logs\watchdog.pid"
$checkIntervalSec = 20
$httpTimeoutSec = 8

# {port, taskName} for both instances -- paper first, live second.
$instances = @(
    @{ Port = 8080; TaskName = "DashboardApp";     Label = "paper" }
    @{ Port = 8081; TaskName = "DashboardAppLive";  Label = "live"  }
)

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

# Guard against duplicate watchdog loops (e.g. a manual re-run alongside the Startup-folder
# launch) -- same PID-lock-file pattern as event-radar's watchdog, checked from the INSIDE
# rather than relying on an external "is it already running" scan (which is inherently racy).
if (Test-Path $lockFile) {
    $existingPid = Get-Content $lockFile -ErrorAction SilentlyContinue
    if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
        exit
    }
}
Set-Content -Path $lockFile -Value $PID -ErrorAction SilentlyContinue

function Test-DashboardUp($port) {
    # Two checks, either failing means "down": (1) something is actually LISTENING on the
    # port (catches the process having genuinely exited -- this incident's actual cause),
    # and (2) it answers a real HTTP request within a short timeout (catches a HUNG process
    # that still holds the port but stopped responding -- the DIFFERENT 2026-07-24 incident
    # class). Neither check alone covers both failure modes seen in this project's history.
    $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $listening) { return $false }
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:$port/" -TimeoutSec $httpTimeoutSec `
                                  -UseBasicParsing -ErrorAction Stop
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

Write-Log "watchdog started (checking every ${checkIntervalSec}s, instances: paper:8080, live:8081)"

while ($true) {
    try {
        foreach ($inst in $instances) {
            if (-not (Test-DashboardUp $inst.Port)) {
                Write-Log "$($inst.Label) (port $($inst.Port)) not responding -- restarting task $($inst.TaskName)"
                try {
                    Start-ScheduledTask -TaskName $inst.TaskName -ErrorAction Stop
                } catch {
                    Write-Log "Start-ScheduledTask threw for $($inst.TaskName): $_"
                }
                Start-Sleep -Seconds 10   # give the launch script's own orphan-guard + startup time to run
                if (Test-DashboardUp $inst.Port) {
                    Write-Log "$($inst.Label) relaunch succeeded"
                } else {
                    Write-Log "$($inst.Label) relaunch not yet confirmed -- will re-check next cycle"
                }
            }
        }
    } catch {
        Write-Log "watchdog loop iteration threw, continuing: $_"
    }
    Start-Sleep -Seconds $checkIntervalSec
}
