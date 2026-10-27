"""Shared test fixtures."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from power_watch.state import new_state, step

INTERVAL = 15
OFF_AFTER = 4
T0 = 1_793_000_000
DEVICE_BOOT = T0 - 3_600  # device on for an hour when the watch starts
KYIV = ZoneInfo("Europe/Kyiv")


def kyiv(h: int, m: int, day: int = 27) -> float:
    return datetime(2026, 10, day, h, m, tzinfo=KYIV).timestamp()


class Probes:
    """Drives state.step() one probe at a time on a 15 s clock."""

    def __init__(self, **saved):
        self.now = T0
        self.st = {**new_state(T0), **saved}
        self.events: list = []

    def probe(self, boot=None, gw_ok=True, n=1):
        for _ in range(n):
            up = None if boot is None else self.now - boot
            self.events += step(self.st, up, gw_ok, self.now, INTERVAL, OFF_AFTER)
            self.now += INTERVAL
        return self

    def device(self, boot=DEVICE_BOOT, n=1):
        return self.probe(boot, True, n)

    def silent(self, n=1):
        return self.probe(None, True, n)

    def router_down(self, n=1):
        return self.probe(None, False, n)

    def kinds(self):
        return [k for k, _ in self.events]
