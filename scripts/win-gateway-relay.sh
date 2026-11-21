#!/bin/sh
# Relay for the LIVE IB Gateway running natively on WINDOWS (spec GATEWAY_PASSKEY, 2026-10-13).
#
# Why the live gateway left Docker: IBKR's login requires the YubiKey PIN on EVERY gateway login
# (userVerification=REQUIRED, measured in Windows' WebAuthN log), and only Windows' own WebAuthn
# dialog can collect a PIN -- the Linux gateway's embedded browser has no PIN UI.
#
# Runs in a container with `network_mode: host`, i.e. inside the WSL2 VM's own network namespace.
# WSL2 is in NAT mode, so Windows is this VM's DEFAULT GATEWAY -- an address that can change on
# every WSL restart. It is therefore resolved here at start (from /proc/net/route, no `ip` binary
# needed) rather than hardcoded anywhere. A WSL restart restarts this container too, so the
# address is re-resolved whenever it can have changed.
#
# The live dashboard reaches this relay at host.docker.internal:${LISTEN_PORT}. 4003 on purpose:
# ib_exec._guard() only arms live trading on ports 7496/4001/4003 (4003 = the Docker gateway's
# old socat relay port), so reusing it keeps that real-money safety check unchanged.
LISTEN_PORT=${LISTEN_PORT:-4003}
WIN_PORT=${WIN_PORT:-4001}
hexgw=$(awk '$2 == "00000000" { print $3; exit }' /proc/net/route)
if [ -z "$hexgw" ]; then echo "relay: no default route in /proc/net/route"; exit 1; fi
# /proc/net/route stores the gateway little-endian hex: 0101A8C0 -> 192.168.1.1
WIN_IP=$(printf '%d.%d.%d.%d' "0x$(echo "$hexgw" | cut -c7-8)" "0x$(echo "$hexgw" | cut -c5-6)" \
                              "0x$(echo "$hexgw" | cut -c3-4)" "0x$(echo "$hexgw" | cut -c1-2)")
echo "relay: 0.0.0.0:${LISTEN_PORT} -> Windows ${WIN_IP}:${WIN_PORT}"
exec socat TCP-LISTEN:"${LISTEN_PORT}",fork,reuseaddr TCP:"${WIN_IP}":"${WIN_PORT}"
