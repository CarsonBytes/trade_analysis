#!/usr/bin/env python3
"""Set WSL's eth0 MTU without needing sudo/root on the host.

Invoked by scripts/fix-wsl-mtu.sh from a container sharing WSL's network
namespace (--network host), where CAP_NET_ADMIN lets us call SIOCSIFMTU.
`ip` is not installed in the app image, hence the ioctl rather than
`ip link set eth0 mtu`.

MTU is passed as argv[1] (default 1380 = the SurfsharkWireGuard path MTU;
a value LOWER than the real path always works, a higher one blackholes,
so 1380 is safe with or without the VPN).
"""
import fcntl
import socket
import struct
import sys

SIOCGIFMTU = 0x8921
SIOCSIFMTU = 0x8922
NAME = b"eth0"


def current(s):
    raw = fcntl.ioctl(s, SIOCGIFMTU, struct.pack("16sI24x", NAME, 0))
    return struct.unpack("16sI24x", raw)[1]


def main():
    wanted = int(sys.argv[1]) if len(sys.argv) > 1 else 1380
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    before = current(sock)
    if before != wanted:
        fcntl.ioctl(sock, SIOCSIFMTU, struct.pack("16sI24x", NAME, wanted))
    print("eth0 MTU %d -> %d" % (before, current(sock)))


if __name__ == "__main__":
    main()