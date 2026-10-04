#!/bin/bash
# Keep WSL's eth0 MTU in sync with the real path MTU. Idempotent, cheap, and
# safe to run from cron every minute: it only does anything when the MTU is
# actually wrong.
#
# WHY THIS EXISTS: WSL's eth0 always comes up at 1500, but when the Windows
# default route is a VPN tunnel (SurfsharkWireGuard, MTU 1380) every outbound
# segment >1380B is silently dropped. That does NOT look like a normal outage:
# TCP still connects (SYN is small), so you get TLS hanging right after
# ClientHello, large HTTP bodies delivering 0 bytes, and `docker compose
# build` failing with "TLS handshake timeout" pulling base images. On
# 2026-10-04 that silently broke the entire auto-deploy pipeline while every
# push kept logging "deploy OK".
#
# sudo needs a password on this box, so the MTU is set from a container that
# shares WSL's network namespace (--network host => eth0 inside the container
# IS WSL's eth0) with CAP_NET_ADMIN. See scripts/set-mtu-ioctl.py.
set -u
WANTED=${WSL_ETH0_MTU:-1380}
CUR=$(cat /sys/class/net/eth0/mtu 2>/dev/null || echo 0)

[ "$CUR" = "$WANTED" ] && exit 0

# Resolve the directory holding set-mtu-ioctl.py from our own location, so the
# script behaves identically whether cron runs the /home/cap/quant rsync copy
# or the /mnt/d/quant source copy.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -f "$SCRIPT_DIR/set-mtu-ioctl.py" ] || {
    echo "set-mtu-ioctl.py missing next to $0" >&2
    exit 1
}

printf '%s eth0 MTU is %s, want %s -- fixing\n' \
    "$(date '+%F %T')" "$CUR" "$WANTED" >> /home/cap/wsl-mtu.log

docker run --rm --network host --cap-add=NET_ADMIN \
    -v "$SCRIPT_DIR":/scripts:ro \
    quant-dashboard:latest python /scripts/set-mtu-ioctl.py "$WANTED" \
    >> /home/cap/wsl-mtu.log 2>&1 || {
        echo "  FIX FAILED" >> /home/cap/wsl-mtu.log
        exit 1
    }