#!/bin/sh
# Runs INSIDE a gateway container (piped in: `docker exec -i <c> sh -s < this-file`).
# Read-only. Prints one verdict about whether an automatic relogin can possibly succeed:
#   PASSKEY_ONLY  the last login handshake offered ONLY passkey second factors -- a headless
#                 gateway cannot complete that unattended (spec GATEWAY_PASSKEY, 2026-10-06)
#   THROTTLED     IBKR answered "Too many failed login attempts" after the last handshake --
#                 every further attempt extends the lockout
#   (nothing)     no blocker found -- an automatic relogin is worth trying
#
# Evidence format, verified in launcher.log on 2026-10-06 (105/105 handshakes):
#   NS_AUTH_START version=51 pwd=1 soft=0 token(s)=[token=5, tokenSubtype=4[Passkey]] ...
# The push era advertised a different subtype; anything that is not "Passkey" counts as a
# factor automation can still use, so a mixed list is NOT treated as passkey-only.
# Newest log that actually CONTAINS a handshake: launcher.log rotates daily, and a day where
# IBKR reset the connection before auth (seen 2026-10-06: 0 handshakes, 161 resets) would
# otherwise hide the last real answer about which factors the account offers.
f=""; last=""
for c in $(ls -t /home/ibgateway/tws_settings/launcher*.log /home/ibgateway/Jts/launcher*.log 2>/dev/null); do
    last=$(grep -n 'NS_AUTH_START' "$c" | tail -1)
    if [ -n "$last" ]; then f="$c"; break; fi
done
[ -n "$f" ] || exit 0
n=${last%%:*}
subtypes=$(printf '%s\n' "$last" | grep -oE 'tokenSubtype=[0-9]+\[[^]]*\]')
if [ -n "$subtypes" ] && ! printf '%s\n' "$subtypes" | grep -qv 'Passkey'; then
    echo PASSKEY_ONLY
    exit 0
fi
if tail -n +"$n" "$f" | grep -qi 'too many failed login'; then
    echo THROTTLED
fi
exit 0
