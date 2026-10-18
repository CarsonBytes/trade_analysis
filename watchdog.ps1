# Windows-side watchdog for BOTH quant dashboards as served by the WSL2/Docker deployment
# (paper http://localhost:18080, live http://localhost:18081). Runs indefinitely, checking
# every 20s from OUTSIDE WSL.
#
# REPOINTED 2026-09-12. Until now this watched the NATIVE Windows deployment (ports 8080/8081,
# Start-ScheduledTask DashboardApp / DashboardAppLive). That deployment is retired: both tasks
# are Disabled, nothing listens on 8080/8081, and quant.carsonng.com points at :18080. The
# old script had been failing every 20s before it died -- its own log, last written
# 2026-08-13, reads:
#     paper (port 8080) not responding -- restarting task DashboardApp
#     Start-ScheduledTask threw for DashboardApp: <task is disabled>
# Restoring THAT verbatim would have reinstated a no-op loop, and would have become an active
# hazard the moment those tasks were re-enabled (a native dashboard and the Docker one fight
# over the same IBKR session -- see project-quant-dashboard's tug-of-war note).
#
# WHY THIS EXISTS AT ALL, given infra-watchdog.sh already supervises these containers:
# infra-watchdog.sh runs INSIDE WSL, on WSL's own cron. It therefore cannot detect the failure
# class where WSL or Docker itself is the broken thing -- most notably the confirmed incident
# where containers were healthy and reachable on the WSL IP while Windows `localhost`
# forwarding had silently stopped working, which took every Docker site on this machine down
# until `wsl --shutdown`. From inside WSL that looks perfectly healthy. This script is the
# outside observer for exactly that blind spot.
#
# DELIBERATELY COMPLEMENTARY, NOT DUPLICATIVE. infra-watchdog.sh runs every 60s and owns
# single-container recovery. This one waits $containerGraceCycles before touching an
# individual container, so the in-WSL watchdog gets first refusal and the two never
# double-restart. Only the "BOTH dashboards unreachable from Windows" signal -- the one
# infra-watchdog.sh structurally cannot see -- is escalated here.
#
# NOT auto-installed -- this file alone changes nothing. See install-watchdog.ps1, or copy
# watchdog-start.vbs into the Startup folder.

$logFile  = "D:\quant\logs\watchdog.log"
$lockFile = "D:\quant\logs\watchdog.pid"
$checkIntervalSec = 20
$httpTimeoutSec   = 8

# How many consecutive failed cycles before this script acts. The container grace exists so
# infra-watchdog.sh (60s cadence) always gets first attempt; ~2min means it has had at least
# two passes before we intervene at all.
$containerGraceCycles = 6     # ~2 min  -> restart that one container
$wslGraceCycles       = 15    # ~5 min  -> investigate WSL/Docker itself
$actionCooldownCycles = 15    # ~5 min  -> don't re-act on the same target while it settles

# The big hammer, OFF by default and intentionally so: `wsl --shutdown` drops EVERY container
# on this machine (quant paper+live, study-platform, event-radar, llm-usage-dashboard,
# whatsapp), and on the quant gateways it forces a fresh IBKR login with a 2FA push. That is
# not a decision a background loop should take unattended -- it alerts and names the command
# instead. Flip to $true only if you have decided you want it automated.
$allowWslShutdown = $false

$distro = "Ubuntu"

$instances = @(
    @{ Port = 18080; Container = "quant-dashboard-docker";      Label = "paper" }
    @{ Port = 18081; Container = "quant-dashboard-live-docker";  Label = "live"  }
)

New-Item -ItemType Directory -Force -Path (Split-Path $logFile) -ErrorAction SilentlyContinue | Out-Null

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

# Best-effort push through the same Telegram/ntfy helper the in-WSL watchdogs use, so alerts
# from here land in the same place. By contract a failed push must never break the loop --
# and note it runs THROUGH WSL, so it is itself unavailable in the worst case this script is
# built to detect. That is why every alert is written to the log first, unconditionally.
# NOTIFICATION_SPEC (2026-10-04): three args now -- <level> <key> <msg>. Only `critical`
# reaches Telegram; `error` lands in the shared audit trail for the daily digest instead.
# gateway-push.sh still accepts the old 1-arg form (audited as key="legacy") so an
# un-updated caller cannot silently stop alerting, but it is no longer the contract.
function Send-Alert($level, $key, $msg) {
    try {
        # wsl.exe arg marshalling eats backslashes -- same fix as
        # restart-verify.ps1's Send-Push (paths used to arrive as C:IBC...).
        $msg = "$msg".Replace('\', '/')
        & wsl.exe -d $distro -- bash /home/cap/quant/scripts/gateway-push.sh "$level" "$key" "$msg" 2>$null | Out-Null
    } catch { }
}

# --- GATEWAY/RELAY LAYER (spec MIN-2FA, 2026-10-17) --------------------------------
# Added after the 2026-10-16..17 outage where BOTH went unnoticed for ~28h each: the
# gateway window closed Oct 16 10:25 (nobody told) and the API Relay task died with
# 0xC000013A the same minute. This layer detects either, toasts, pages Telegram (critical
# key -> one buzz per incident via gateway-push.sh's 15min per-key cooldown), and
# auto-launches the passkey login so the prompt is waiting at the PC.
$gatewayGraceCycles = 6   # ~2 min: a healthy 20:30 token restart blips 30-60s -- must NOT alarm
$relayGraceCycles   = 6

function Test-GatewayUp {
    # BOTH conditions: port 4001 listening AND our IBC-Live java process exists. The port
    # alone can be transient during startup; the process alone can linger after a crash.
    if (-not (Get-NetTCPConnection -LocalPort 4001 -State Listen -ErrorAction SilentlyContinue)) { return $false }
    $ibc = Get-CimInstance Win32_Process -Filter "Name='java.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'IBC-Live' }
    return [bool]$ibc
}

function Test-RelayUp {
    # ONLY an IPv4 0.0.0.0:4003 listener owned by powershell counts. wslrelay.exe binds
    # ::1:4003 (WSL localhost forwarding) and made a naive port check report false-UP for
    # the entire 28h Oct 16-17 relay outage -- the LocalAddress + owner filters exclude it.
    $l = Get-NetTCPConnection -LocalPort 4003 -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -eq '0.0.0.0' } | Select-Object -First 1
    if (-not $l) { return $false }
    $own = Get-CimInstance Win32_Process -Filter "ProcessId=$($l.OwningProcess)" -ErrorAction SilentlyContinue
    return [bool]($own -and $own.Name -match '^powershell')
}

function Test-LoginInProgress {
    $p = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'ibkr-live-login\.ps1' }
    return [bool]$p
}

# Detached so the balloon's 16s lifetime never blocks this loop. Quotes are embedded in the
# argument elements because Start-Process does not quote args containing spaces itself.
function Show-Toast($title, $text) {
    try {
        Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', 'D:\quant\scripts\windows\toast.ps1',
            '-Title', ('"{0}"' -f $title), '-Text', ('"{0}"' -f $text)
        ) | Out-Null
    } catch { Write-Log "toast launch threw: $_" }
}

function Start-LiveLogin {
    try {
        Start-Process powershell.exe -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', 'D:\quant\scripts\windows\ibkr-live-login.ps1'
        ) -WorkingDirectory 'D:\quant' | Out-Null
        return $true
    } catch { Write-Log "login auto-launch threw: $_"; return $false }
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
    # port, and (2) it answers a real HTTP request within a short timeout (catches a HUNG
    # process that still holds the port but stopped responding -- the 2026-07-24 incident
    # class). Neither check alone covers both failure modes seen in this project's history.
    # Under WSL2 the Windows-side listener is the port proxy, so a broken-forwarding failure
    # shows up here too -- which is the whole point of probing from Windows.
    $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $listening) { return $false }
    try {
        # FAVICON, not "/" (2026-10-18): a full page render takes 3.3-11s+ PER CONNECTED
        # BROWSER CLIENT (documented in docker-compose.live.yml's healthcheck comment), so
        # a root fetch with a short timeout false-positives "down" during routine renders
        # -- that misfire docker-restarted the LIVE container at 12:05:07 while it was
        # serving fine (proven: in-container favicon answered clean, only the host root
        # fetch was slow). favicon.ico is static and answers in ~0.011s whenever the event
        # loop is alive -- the actual failure class this watchdog exists to catch. Same
        # endpoint the container healthcheck already uses, so the layers agree.
        $resp = Invoke-WebRequest -Uri "http://localhost:$port/favicon.ico" -TimeoutSec $httpTimeoutSec `
                                  -UseBasicParsing -ErrorAction Stop
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Test-WslRunning {
    try {
        # `wsl -l --running` emits UTF-16; stripping nulls keeps the match reliable.
        $out = (& wsl.exe -l --running) -join " "
        return (($out -replace "`0", "") -match $distro)
    } catch { return $false }
}

function Test-DockerResponding {
    try {
        & wsl.exe -d $distro -- docker ps -q 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$fail = @{}                     # label -> consecutive failed cycles
$cooldown = @{}                 # label -> cycles remaining before we may act again
foreach ($i in $instances) { $fail[$i.Label] = 0; $cooldown[$i.Label] = 0 }
$bothFail = 0
$wslCooldown = 0

# gateway/relay layer state (MIN-2FA)
$gwFail = 0                     # consecutive cycles with gateway down
$gwCooldown = 0                 # cycles before we may re-launch/re-alert
$relayFail = 0
$relayCooldown = 0

Write-Log ("watchdog started (every ${checkIntervalSec}s; paper:18080, live:18081; " +
           "container grace ${containerGraceCycles} cycles, WSL grace ${wslGraceCycles}, " +
           "allowWslShutdown=$allowWslShutdown)")

while ($true) {
    try {
        # --- gateway + relay checks first, every cycle, independent of dashboard state ---
        if ($gwCooldown -gt 0) { $gwCooldown-- }
        if ($relayCooldown -gt 0) { $relayCooldown-- }

        if (Test-GatewayUp) {
            if ($gwFail -ge $gatewayGraceCycles) {
                Write-Log "LIVE gateway RECOVERED (4001 + IBC back)"
                # `error`, not `critical`: recovery pages nobody (NOTIFICATION_SPEC) but stays
                # digest-eligible, same convention as the dashboard layer's restart notices.
                Send-Alert "error" "gateway:recovered" "LIVE gateway recovered (4001+IBC)"
            } elseif ($gwFail -gt 0) { Write-Log "LIVE gateway responding again" }
            $gwFail = 0
        } else {
            $gwFail++
            if ($gwFail -eq 1) {
                Write-Log "LIVE gateway down (4001 or IBC-Live missing) -- ${gatewayGraceCycles}-cycle grace (~2min)"
            } elseif ($gwFail -eq $gatewayGraceCycles) {
                Write-Log "LIVE gateway still down after grace -- toast + critical alert + launch passkey prompt"
                Show-Toast "LIVE IB Gateway down" "No API on 4001 / IBC not running -- launching the passkey prompt; touch the YubiKey when it appears."
                Send-Alert "critical" "gateway:ibc4001" "LIVE gateway down >2min (4001/IBC missing) -- launching the passkey login; touch the YubiKey"
                $gwCooldown = $actionCooldownCycles
                if (Test-LoginInProgress) {
                    Write-Log "a login attempt is already in progress (autostart/manual) -- not double-launching"
                } elseif (Start-LiveLogin) {
                    Write-Log "auto-launched ibkr-live-login.ps1 (gateway-down incident)"
                }
            } elseif ($gwFail -gt $gatewayGraceCycles -and $gwCooldown -le 0) {
                # Still down ~5min after the last action: re-prompt ONLY if no login attempt
                # is running (the previous one timed out at 210s). Login guards make a launch
                # against a pending 2FA prompt adopt it instead of duplicating IBC.
                $gwCooldown = $actionCooldownCycles
                if (Test-LoginInProgress) {
                    Write-Log "gateway still down; login still in progress (human may be fetching the key) -- waiting"
                } else {
                    Write-Log "gateway still down and no login in progress -- re-launching passkey prompt"
                    Send-Alert "critical" "gateway:ibc4001" "LIVE gateway still down after $([int]($gwFail * $checkIntervalSec / 60))min -- re-launching the passkey login"
                    Show-Toast "LIVE IB Gateway still down" "Re-launching the passkey prompt -- touch the YubiKey when it appears."
                    if (Start-LiveLogin) { Write-Log "auto-launched ibkr-live-login.ps1 (re-prompt)" }
                }
            }
        }

        if (Test-RelayUp) {
            if ($relayFail -ge $relayGraceCycles) {
                Write-Log "API relay RECOVERED (0.0.0.0:4003 powershell listener back)"
                Send-Alert "error" "gateway:relay-recovered" "API relay recovered (0.0.0.0:4003)"
            } elseif ($relayFail -gt 0) { Write-Log "API relay responding again" }
            $relayFail = 0
        } else {
            $relayFail++
            if ($relayFail -eq 1) {
                Write-Log "API relay down (no 0.0.0.0:4003 powershell listener) -- ${relayGraceCycles}-cycle grace (~2min)"
            } elseif ($relayFail -ge $relayGraceCycles -and $relayCooldown -le 0) {
                $relayCooldown = $actionCooldownCycles
                Write-Log "API relay still down after grace -- toast + critical alert + Start-ScheduledTask"
                Show-Toast "IBKR API relay down" "Windows relay on 4003 is not running -- restarting task '\IBKR\API Relay'."
                Send-Alert "critical" "gateway:relay4003" "API relay (0.0.0.0:4003) down >2min -- restarting task \IBKR\API Relay"
                try {
                    Start-ScheduledTask -TaskPath '\IBKR\' -TaskName 'API Relay'
                    Write-Log "Start-ScheduledTask '\IBKR\API Relay' issued"
                } catch { Write-Log "relay restart threw: $_" }
            }
        }

        $state = @{}
        foreach ($inst in $instances) { $state[$inst.Label] = Test-DashboardUp $inst.Port }
        $downLabels = @($instances | Where-Object { -not $state[$_.Label] } | ForEach-Object { $_.Label })

        foreach ($k in @($cooldown.Keys)) { if ($cooldown[$k] -gt 0) { $cooldown[$k]-- } }
        if ($wslCooldown -gt 0) { $wslCooldown-- }

        if ($downLabels.Count -eq 0) {
            if ($bothFail -gt 0 -or @($fail.Values | Where-Object { $_ -gt 0 }).Count -gt 0) {
                Write-Log "all dashboards responding again"
            }
            foreach ($i in $instances) { $fail[$i.Label] = 0 }
            $bothFail = 0
            Start-Sleep -Seconds $checkIntervalSec
            continue
        }

        foreach ($l in $downLabels) { $fail[$l]++ }

        if ($downLabels.Count -eq $instances.Count) {
            # BOTH unreachable from Windows. This is the signal infra-watchdog.sh cannot see,
            # so investigate the layers underneath before blaming either container.
            $bothFail++
            if ($bothFail -eq 1) {
                Write-Log "BOTH dashboards unreachable from Windows -- starting WSL-level assessment"
            }
            if ($bothFail -ge $wslGraceCycles -and $wslCooldown -le 0) {
                $wslCooldown = $actionCooldownCycles
                if (-not (Test-WslRunning)) {
                    Write-Log "WSL distro '$distro' is NOT running -- starting it"
                    Send-Alert "critical" "wsl:distro" "quant: WSL ($distro) was not running -- watchdog started it"
                    try { & wsl.exe -d $distro -- true 2>$null | Out-Null } catch { }
                } elseif (-not (Test-DockerResponding)) {
                    Write-Log "WSL is up but Docker is not responding -- attempting to start it"
                    Send-Alert "critical" "wsl:docker" "quant: Docker inside WSL is not responding -- watchdog attempting start"
                    try { & wsl.exe -d $distro -u root -- service docker start 2>$null | Out-Null } catch { }
                } else {
                    # Docker is fine, and the containers are presumably fine with it -- yet
                    # Windows cannot reach either port. That is the localhost-forwarding
                    # failure class, and no amount of container restarting fixes it.
                    Write-Log ("Docker is responding but NEITHER dashboard is reachable on Windows " +
                               "localhost -- this is the WSL2 localhost-forwarding failure class. " +
                               "Containers are likely healthy on the WSL IP. Fix: wsl --shutdown " +
                               "(drops ALL containers on this machine and forces a fresh IBKR " +
                               "login with 2FA), after which they restart on their own.")
                    Send-Alert "critical" "wsl:forwarding" ("quant: both dashboards unreachable from Windows while Docker is " +
                                "HEALTHY -- WSL localhost forwarding has failed. Needs wsl --shutdown.")
                    if ($allowWslShutdown) {
                        Write-Log "allowWslShutdown is set -- running wsl --shutdown"
                        try { & wsl.exe --shutdown 2>$null | Out-Null } catch { }
                        Start-Sleep -Seconds 20
                        try { & wsl.exe -d $distro -- true 2>$null | Out-Null } catch { }
                    }
                }
            }
            Start-Sleep -Seconds $checkIntervalSec
            continue
        }

        # Exactly one is down, so WSL, Docker and port forwarding are all demonstrably fine
        # (the other dashboard answered over the same path). That makes it a container-level
        # problem, which infra-watchdog.sh owns -- hold off until the grace period proves it
        # is not recovering on its own.
        $bothFail = 0
        foreach ($inst in $instances) {
            $l = $inst.Label
            if ($state[$l]) { continue }
            if ($fail[$l] -eq 1) {
                Write-Log "$l (port $($inst.Port)) not responding -- infra-watchdog.sh gets first attempt"
            }
            if ($fail[$l] -lt $containerGraceCycles -or $cooldown[$l] -gt 0) { continue }
            $mins = [int]($fail[$l] * $checkIntervalSec / 60)
            Write-Log ("$l still down after $($fail[$l]) cycles -- restarting container " +
                       "$($inst.Container) from the Windows side")
            # NOTIFICATION_SPEC: `error`, not `critical` -- the restart itself succeeded, so
            # there is nothing to act on now. infra-watchdog.sh records its own restarts the
            # same way, so both layers feed one digest.
            Send-Alert "error" "restart:$l" "quant: $l dashboard down ~${mins}min and not recovered by infra-watchdog -- restarting $($inst.Container)"
            try {
                & wsl.exe -d $distro -- docker restart $($inst.Container) 2>$null | Out-Null
            } catch {
                Write-Log "docker restart threw for $($inst.Container): $_"
            }
            $cooldown[$l] = $actionCooldownCycles
            Start-Sleep -Seconds 15
            if (Test-DashboardUp $inst.Port) {
                Write-Log "$l recovered after container restart"
                $fail[$l] = 0
            } else {
                Write-Log "$l restart not yet confirmed -- will re-check next cycle"
            }
        }
    } catch {
        Write-Log "watchdog loop iteration threw, continuing: $_"
    }
    Start-Sleep -Seconds $checkIntervalSec
}
