"""The service: probe every interval, feed the state machine, post and persist what it reports."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from types import ModuleType
from typing import Any, NamedTuple

from power_watch import probe, store
from power_watch.bot import Commands
from power_watch.config import Config
from power_watch.messages import message, status_text
from power_watch.state import CLOCK_STEP_S, describe, new_state, shift_times, step
from power_watch.telegram import Telegram

log = logging.getLogger(__name__)

# How often to look for a silent sensor somewhere else on the network (a scan takes 2–5 s), and
# for the purifier. While the power is out, look more often, so a return at a new address is seen.
SEARCH_EVERY_S = 300
SEARCH_EVERY_OFF_S = 60
# How often to repeat the error about an unknown device at the sensor's address.
WARN_EVERY_S = 1800


class LoadedPurifier(NamedTuple):
    module: ModuleType  # power_watch.purifier, imported only when configured
    device: Any  # power_watch.purifier.Device


def wait_for_clock() -> None:
    """Boards without an RTC (Raspberry Pi) boot with a stale clock, and every boot time is
    now - uptime: probing before NTP would post a power return that never happened. Waits for
    systemd-timesyncd's sync flag when timesyncd is in use, however long it takes (no network
    means no posts either); other setups should order the unit after time-sync.target
    (systemd-time-wait-sync)."""
    flag = "/run/systemd/timesync/synchronized"
    if not os.path.isdir("/run/systemd/timesync") or os.path.exists(flag):
        return
    waited = 0
    while not os.path.exists(flag):
        if waited % 600 == 0:
            log.info("power-watch: waiting for the clock to sync")
        time.sleep(5)
        waited += 5


def load_purifier(cfg: Config) -> LoadedPurifier | None:
    """The purifier module and its Device when one is configured and loadable. Optional by design:
    without a working python3-cryptography the power watch still runs, minus the readings."""
    if not (cfg.purifier_ip and cfg.purifier_token):
        if cfg.purifier_ip or cfg.purifier_token:
            log.warning("power-watch: purifier needs both POWER_WATCH_PURIFIER_IP and POWER_WATCH_PURIFIER_TOKEN")
        return None
    try:
        from power_watch import purifier
    except Exception as e:  # noqa: BLE001 — a missing or broken cryptography must never stop the watch
        if isinstance(e, ModuleNotFoundError) and (e.name or "").startswith("cryptography"):
            log.warning("power-watch: purifier readings off — install python3-cryptography")
        else:
            log.warning("power-watch: purifier readings off — %s: %s", type(e).__name__, e)
        return None
    return LoadedPurifier(purifier, purifier.Device(cfg.purifier_ip, cfg.purifier_token))


class Watch:
    """The running service. The constructor restores the persisted state (and its unsent posts)
    and loads the optional purifier; run() starts the Telegram threads and probes forever."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.live_path = os.path.join(cfg.run_dir, "state") if cfg.run_dir else ""
        self.persist_path = os.path.join(cfg.state_dir, "state.json") if cfg.state_dir else ""
        self.st = new_state(int(time.time()))
        outbox = store.load_state(self.persist_path, self.st) if self.persist_path else []
        # A different sensor has a different power-on time: its first answer must be a silent
        # baseline, not a "power is back" post. But the same lamp under a new configured IP, or an
        # id set before any was learned (e.g. during an outage), keeps the state and its return post.
        # (prev_device_id is saved, so a restart before that first answer doesn't lose it.)
        known_id = self.st["device_id"]
        if self.st["device_cfg"] != cfg.device:
            self.st.update(device_id=0, device_cfg=cfg.device, prev_device_id=known_id)
        if cfg.device_id:
            if known_id and cfg.device_id != known_id:
                self.rebaseline()
            self.st.update(device_id=cfg.device_id, prev_device_id=0)
        # A new purifier always comes with a new token: key the remembered id on both (a hash only,
        # the token itself never leaves the settings file).
        purifier_key = f"{cfg.purifier_ip} {hashlib.sha256(cfg.purifier_token.encode()).hexdigest()[:12]}"
        if self.st["purifier_cfg"] != purifier_key:
            self.st.update(purifier_id=0, purifier_cfg=purifier_key)
        self.device_ip = cfg.device  # where the sensor answers now; follows it when DHCP moves it
        self.last_search = -1e9  # monotonic time of the last network search for the sensor
        self.next_purifier_check = 0.0
        self.next_warning = 0.0
        self.other_id = 0  # the last unknown device seen at the sensor's address
        self.lock = threading.Lock()
        self.tg = Telegram(cfg.token, cfg.chat, outbox, on_sent=self.persist) if cfg.token and cfg.chat else None
        self.purifier = load_purifier(cfg)
        if self.purifier and not self.purifier.device.did:
            self.purifier.device.did = self.st["purifier_id"]  # to find it even before it answers
        if self.purifier and not cfg.token:
            log.warning(
                "power-watch: a purifier is set, but its readings only appear in the bot's /status "
                "(no TELEGRAM_BOT_TOKEN)"
            )

    def persist(self) -> None:
        if not self.persist_path:
            return
        with self.lock:
            try:
                store.save_state(self.persist_path, self.st, self.tg.pending() if self.tg else [])
            except OSError as e:  # keep watching: dying here would re-post the same event on restart
                log.error("power-watch: cannot save state: %s", e)

    def status(self) -> str:
        """The /status reply: the power line, then the purifier's lines when one is set."""
        text = status_text(self.st, self.cfg.lang, self.cfg.tz, time.time(), 3 * self.cfg.interval)
        if not self.purifier:
            return text
        return "\n".join([text, *self.purifier.module.status(self.purifier.device, self.cfg.lang)])

    def sensor_check(self) -> tuple[str, int | None]:
        """("up", uptime) when the sensor answers, ("silent", None) when nothing does, and
        ("other", None) when a different device answers at its address: DHCP gave the address to
        someone else, or the sensor was replaced (only the user can tell; see check_other)."""
        reply = probe.hello(self.device_ip)
        if not reply:
            return "silent", None
        device_id, up = reply
        if not self.st["device_id"]:
            self.st["device_id"] = device_id  # first answer: remember who the sensor is
            if self.st["prev_device_id"] and device_id != self.st["prev_device_id"]:
                self.rebaseline()
            self.st["prev_device_id"] = 0
            self.persist()
        elif device_id != self.st["device_id"]:
            self.other_id = device_id
            return "other", None
        return "up", up

    def rebaseline(self) -> None:
        """A new sensor: forget the old one's power state (the next answer is a silent baseline)."""
        self.st.update(power="unknown", last_on=0, misses=0)

    def search_due(self, kind: str) -> bool:
        """Rate-limited, except right before a silence would be declared an outage: a sensor that
        rejoined Wi-Fi at a new address since the last search must be found before that. (Only for
        silence: an unknown device's answers never count toward "out", and the miss count stands
        still meanwhile, which would force a search on every probe.)"""
        if kind == "silent" and self.st["power"] != "off" and self.st["misses"] == self.cfg.off_after - 1:
            return True  # (this probe makes the declaring miss if it is silent)
        every = SEARCH_EVERY_OFF_S if self.st["power"] == "off" else SEARCH_EVERY_S
        return time.monotonic() - self.last_search >= every

    def warn_other(self) -> None:
        if time.monotonic() < self.next_warning:
            return
        self.next_warning = time.monotonic() + WARN_EVERY_S
        log.error(
            "power-watch: %s answers as device %s, but the sensor is device %s and isn't anywhere on "
            "the network, so these answers are ignored (no outage is declared from them). If you "
            "replaced the sensor, set POWER_WATCH_DEVICE_ID=%s in /etc/power-watch.env and restart.",
            self.device_ip,
            self.other_id,
            self.st["device_id"],
            self.other_id,
        )

    def search(self, device_id: int, current_ip: str, what: str) -> str | None:
        """The device's new IP if it has moved, else None. Callers rate-limit it."""
        new_ip = probe.find(device_id, current_ip)
        if new_ip and new_ip != current_ip:
            log.warning(
                "power-watch: the %s moved %s → %s; following it (a DHCP reservation in the "
                "router keeps its address fixed)",
                what,
                current_ip,
                new_ip,
            )
            return new_ip
        return None

    def probe_once(self, now: float) -> None:
        cfg = self.cfg
        kind, up = self.sensor_check()
        gw_ok = True
        if kind != "up":
            # Re-resolved every probe: at boot the default route may not exist yet.
            gateway = cfg.gateway or probe.default_gateway()
            gw_ok = kind == "other" or (bool(gateway) and probe.gateway_up(gateway, cfg.gw_port))
            # Not the sensor answering while the router does: before that counts toward "out",
            # check the sensor hasn't simply been given a new address.
            if gw_ok and self.st["device_id"] and self.search_due(kind):
                self.last_search = time.monotonic()
                new_ip = self.search(self.st["device_id"], self.device_ip, "sensor")
                if new_ip:
                    self.device_ip = new_ip
                    kind, up = self.sensor_check()
        self.follow_purifier()
        if kind == "other":
            # A different device at the sensor's address, and the sensor nowhere: no data. Never
            # declare an outage from it (a replaced sensor would make that permanent), and break the
            # silence streak: later silence must count from scratch, not finish an old count.
            self.warn_other()
            self.st["misses"] = 0
            events = []
        else:
            events = step(self.st, up, gw_ok, now, cfg.interval, cfg.off_after)
        for kind, ev in events:
            log.info(describe(kind, ev))
            text = message(kind, ev, cfg.lang, cfg.tz, now, cfg.label)
            if self.tg and text:
                self.tg.post(text)
        if events:
            self.persist()
        self.st["checked"] = int(now)
        if self.live_path:
            try:
                store.write_live(self.live_path, self.st)
            except OSError as e:
                log.error("power-watch: cannot write %s: %s", self.live_path, e)

    def follow_purifier(self) -> None:
        """Every few minutes, check the purifier still answers where expected and look for it
        elsewhere if not (from the probe loop, so /status never waits for a scan). Its id comes
        from a handshake (saved, so this works after a restart too)."""
        if not self.purifier:
            return
        dev = self.purifier.device
        if dev.did and dev.did != self.st["purifier_id"]:
            self.st["purifier_id"] = dev.did
            self.persist()
        if not dev.did or time.monotonic() < self.next_purifier_check:
            return
        self.next_purifier_check = time.monotonic() + SEARCH_EVERY_S
        reply = probe.hello(dev.ip)
        if reply and reply[0] == dev.did:
            return
        new_ip = self.search(dev.did, dev.ip, "purifier")
        if new_ip:
            dev.ip = new_ip

    def run(self) -> None:
        cfg = self.cfg
        pending = len(self.tg.pending()) if self.tg else 0  # before the outbox worker starts sending
        if self.tg:
            self.tg.start()
        if cfg.token:
            # Listen even with nobody allowed yet: an unknown user's message logs the id to allow.
            Commands(self.tg or Telegram(cfg.token, ""), cfg.allowed, self.status, cfg.lang).start()
        log.info(
            "power-watch: device %s, gateway %s:%s, every %gs, off after %s misses, channel %s, "
            "bot %s (%d user(s)), purifier %s, starting from power=%s%s",
            cfg.device,
            cfg.gateway or "(default route)",
            cfg.gw_port,
            cfg.interval,
            cfg.off_after,
            "on" if self.tg else "off",
            "on" if cfg.token else "off",
            len(cfg.allowed),
            cfg.purifier_ip if self.purifier else "off",
            self.st["power"],
            f", {pending} unsent post(s)" if pending else "",
        )
        clock_offset = time.time() - time.monotonic()
        while True:
            t0, m0 = time.time(), time.monotonic()
            jump = (t0 - m0) - clock_offset
            clock_offset = t0 - m0
            if abs(jump) > CLOCK_STEP_S:
                log.info("power-watch: wall clock stepped %+.0f s — shifting stored times", jump)
                shift_times(self.st, jump)
                self.persist()
            self.probe_once(t0)
            time.sleep(max(0.0, cfg.interval - (time.monotonic() - m0)))


def watch(cfg: Config) -> int:
    if not cfg.device:
        log.error("power-watch: POWER_WATCH_DEVICE_IP unset — nothing to watch")
        return 2  # the unit's RestartPreventExitStatus: a config error, not worth retrying
    wait_for_clock()
    Watch(cfg).run()
    return 0
