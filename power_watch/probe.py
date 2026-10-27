"""Network probes: the token-free miIO hello, the router check, and a LAN scan for devices."""

from __future__ import annotations

import ipaddress
import socket
import time

MIIO_PORT = 54321
# miIO hello: magic 0x2131, length 32, then 28 bytes of 0xff.
MIIO_HELLO = bytes.fromhex("21310020" + "ff" * 28)


def parse_hello(data: bytes) -> tuple[int, int] | None:
    """(device id, uptime s) from a miIO hello reply, or None if it is not one."""
    if len(data) >= 16 and data[:2] == b"\x21\x31":
        return int.from_bytes(data[8:12], "big"), int.from_bytes(data[12:16], "big")
    return None


def device_uptime(ip: str) -> int | None:
    """Seconds since the device powered on, or None when it does not answer (two tries)."""
    for _ in range(2):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.5)
            try:
                s.sendto(MIIO_HELLO, (ip, MIIO_PORT))
                data, _ = s.recvfrom(1024)
            except OSError:
                continue
        hello = parse_hello(data)
        if hello:
            return hello[1]
    return None


def gateway_up(ip: str, port: int) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=1.5):
            return True
    except ConnectionRefusedError:
        return True  # a reset is still an answer: the router is there, the port just is not open
    except OSError:
        return False


def default_gateway() -> str:
    """The IPv4 default route's gateway, from /proc/net/route (Linux); '' if none."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                    return socket.inet_ntoa(int(fields[2], 16).to_bytes(4, "little"))
    except (OSError, ValueError):
        pass
    return ""


def arp_table() -> dict[str, str]:
    """{IP: MAC} from the kernel's neighbour table (Linux), to name what a scan found."""
    try:
        with open("/proc/net/arp") as f:
            rows = [line.split() for line in f.readlines()[1:]]
        return {row[0]: row[3] for row in rows if len(row) > 3}
    except OSError:
        return {}


def scan(cidr: str, wait_s: float = 3.0) -> dict[str, tuple[int, int]]:
    """{IP: (device id, uptime s)} for every miIO device answering in the network. Unicast hello
    to each host: broadcast replies are often dropped by host firewalls."""
    net = ipaddress.ip_network(cidr, strict=False)
    if net.num_addresses > 1024:
        raise ValueError("scan at most a /22")
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for host in net.hosts():
            try:
                s.sendto(MIIO_HELLO, (str(host), MIIO_PORT))
            except OSError:
                pass
        s.settimeout(0.5)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            try:
                data, (ip, _) = s.recvfrom(1024)
            except OSError:
                continue
            hello = parse_hello(data)
            if hello:
                found[ip] = hello
    return found
