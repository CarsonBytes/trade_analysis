#!/bin/bash
# Stall capture for quant-dashboard-live-docker (added 2026-10-18).
#
# WHY: HTTP :18081's event loop was suspected of intermittent 5-15s freezes (watchdog
# restarts ~hourly). Proven 2026-10-18: those were mostly SLOW FULL-PAGE RENDERS for
# connected browser clients, not freezes -- the in-container favicon kept answering.
# This cron therefore probes the favicon (loop liveness); on a TRUE miss it dumps the
# app's stacks via py-spy (SYS_PTRACE granted in docker-compose.live.yml, install
# self-heals after a container recreate). Rate-limited to DUMP_LIMIT dumps per day.
#
# Deploy: crontab -e, add   * * * * * /home/cap/stall_capture.sh
LOG=/home/cap/stall-capture.log
DUMPDIR=/home/cap/stalls
CNTFILE=/home/cap/stalls/.count
DUMP_LIMIT=8
mkdir -p "$DUMPDIR"

today=$(date +%F)
read -r lastday count < "$CNTFILE" 2>/dev/null || { lastday=""; count=0; }
[ "$lastday" != "$today" ] && count=0

# Probe = IN-CONTAINER favicon: the event-loop liveness signal (same endpoint the
# container healthcheck uses). NOT a host-side root fetch -- a full render takes
# 3.3-11s+ per connected browser client and routinely blows short host budgets
# (proven 2026-10-18 12:02+12:03: host root >8s while this favicon answered clean --
# those were slow renders, not freezes; dumping on them wastes the daily budget).
if docker exec quant-dashboard-live-docker python3 -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8091/favicon.ico', timeout=5)" \
        >/dev/null 2>&1; then
    # loop is serving
    if [ -f "$DUMPDIR/.stalling" ]; then
        echo "$(date '+%F %T') recovered (stall over)" >> "$LOG"
        rm -f "$DUMPDIR/.stalling"
    fi
    exit 0
fi

# True freeze (or the container is gone). Log host root status too -- context for whether
# the freeze is loop-wide or container-wide.
hostcode=$(curl -4 -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:18081" 2>/dev/null)
echo "$(date '+%F %T') IN-CONTAINER favicon FAIL (5s) -- true loop/container stall; host root=$hostcode" >> "$LOG"
touch "$DUMPDIR/.stalling"

if [ "$count" -ge "$DUMP_LIMIT" ]; then
    echo "$(date '+%F %T') dump limit reached ($count/$DUMP_LIMIT today)" >> "$LOG"
    exit 0
fi

# App pids inside the container: every process whose cmdline mentions dashboard.app
# (the `uv run` parent AND the python child -- py-spy errors uselessly on the former,
# dumps stacks from the latter; both land in one file).
pids=$(docker exec quant-dashboard-live-docker sh -c \
    'for d in /proc/[0-9]*; do c=$(tr "\0" " " < "$d/cmdline" 2>/dev/null); case "$c" in *dashboard.app*) echo ${d#/proc/};; esac; done' \
    2>/dev/null)

ts=$(date +%Y%m%d_%H%M%S)
if [ -n "$pids" ]; then
    # Self-heal: a container recreate wipes the writable layer's pip installs.
    docker exec quant-dashboard-live-docker sh -c 'command -v py-spy >/dev/null 2>&1 || pip install -q py-spy' >/dev/null 2>&1
    : > "$DUMPDIR/${ts}_dump.txt"
    for pid in $pids; do
        echo "===== pid=$pid =====" >> "$DUMPDIR/${ts}_dump.txt"
        docker exec quant-dashboard-live-docker py-spy dump --pid "$pid" \
            >> "$DUMPDIR/${ts}_dump.txt" 2>&1
    done
    bytes=$(wc -c < "$DUMPDIR/${ts}_dump.txt" 2>/dev/null || echo 0)
    docker top quant-dashboard-live-docker > "$DUMPDIR/${ts}_top.txt" 2>&1
    echo "$(date '+%F %T') dumped pids=[$(echo $pids | tr '\n' ' ')] (${bytes}B) -> $DUMPDIR/${ts}_dump.txt" >> "$LOG"
else
    echo "$(date '+%F %T') app pid not found in container" >> "$LOG"
fi
echo "$today $((count + 1))" > "$CNTFILE"
