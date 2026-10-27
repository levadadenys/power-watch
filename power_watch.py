#!/usr/bin/env python3
"""power-watch — is mains power on at home? Posts outages to a Telegram channel.

Meant for a small always-on box (a Raspberry Pi) that rides a UPS together with
the router, so the box itself never sees an outage. A mains-powered Xiaomi
Wi-Fi device (a lamp, a plug, a purifier: anything that speaks miIO) does: it
answers the miIO "hello" packet (UDP 54321, no token needed) while there is
power and goes silent when there is none. Each reply also carries the device's
uptime in seconds, so the moment power came back is exact even when the box
was offline at the time (many routers reboot when mains returns and the UPS
switches back, taking the network down for a couple of minutes).

Silence only counts as "power off" while the router answers: when the box's
own link is down the device is unreachable for an unrelated reason.

Stdlib only, Python 3.10+. Config (environment; see power-watch.env.example):
  POWER_WATCH_DEVICE_IP     miIO device on mains power, NOT on the UPS (required)
  TELEGRAM_BOT_TOKEN        bot that posts to the channel (optional: without it,
  TELEGRAM_CHAT_ID          the journal is the only output); chat id or @channel
  POWER_WATCH_LANG          message language: en (default) or uk
  POWER_WATCH_TZ            IANA zone for times in messages, default: system local
  POWER_WATCH_LABEL         optional prefix for every message, e.g. a place name
  POWER_WATCH_GATEWAY       router IP, default: the default route's gateway
  POWER_WATCH_GATEWAY_PORT  TCP port probed on the router, default 53
  POWER_WATCH_INTERVAL_S    probe cadence, default 15
  POWER_WATCH_OFF_AFTER     consecutive misses before "off", default 4 (~1 min); after
                            the router itself was unreachable, it must answer this many
                            probes first, so a router reboot is never read as an outage

Usage:
  power-watch                   run the watch (systemd unit: power-watch.service)
  power-watch --scan 192.168.0.0/24   list miIO devices on the LAN
  power-watch --probe IP        one probe: is the device up, and since when
  power-watch --test-notify     send a test message to the Telegram chat
"""

import argparse
import ipaddress
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, tzinfo

MIIO_PORT = 54321
# miIO hello: magic 0x2131, length 32, then 28 bytes of 0xff.
MIIO_HELLO = bytes.fromhex("21310020" + "ff" * 28)
# A real power cut reboots the device; a boot time that moved by more than this is a power
# return. Within it, the same boot (uptime and clock jitter).
BOOT_JUMP_S = 30

MESSAGES = {
    "en": {
        "off": "⚡ Power is out since {t}.",
        "off_start": "⚡ Power is out (noticed at {t}).",
        "on": "✅ Power is back at {t}, after {d}.",
        "on_nodur": "✅ Power is back at {t}.",
        "unobserved": "✅ Power came back at {t} after a short or unobserved outage.",
        "false_alarm": "↩️ False alarm: power never went out (the sensor only dropped off Wi-Fi).",
        "test": "🔔 power-watch test message.",
        "hm": "{h} h {m} min",
        "m": "{m} min",
    },
    "uk": {
        "off": "⚡ Світла немає з {t}.",
        "off_start": "⚡ Світла немає (помічено о {t}).",
        "on": "✅ Світло повернулося о {t}. Не було {d}.",
        "on_nodur": "✅ Світло повернулося о {t}.",
        "unobserved": "✅ Світло повернулося о {t} після короткого або непоміченого відключення.",
        "false_alarm": "↩️ Хибна тривога: світло не зникало (датчик лише втратив Wi-Fi).",
        "test": "🔔 Тестове повідомлення power-watch.",
        "hm": "{h} год {m} хв",
        "m": "{m} хв",
    },
}

# The runtime state file's keys (key=value lines in $RUNTIME_DIRECTORY/state, rewritten every
# probe): a stable contract for other local readers, e.g. an alert script that wants to know
# whether a network drop was a power return.
PUBLIC_KEYS = ("power", "since", "last_on", "last_off", "last_seen", "checked")
# Kept across restarts in $STATE_DIRECTORY/state.json (written on transitions only, to spare
# SD cards), so a restart neither repeats nor loses an outage.
PERSIST_KEYS = ("power", "since", "last_on", "last_off", "last_seen", "prev_since", "prev_last_off")


def log(msg: str) -> None:
    print(msg, flush=True)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def step(st: dict, up: int | None, gw_ok: bool, now: float, interval: float, off_after: int) -> list[tuple[str, dict]]:
    """Advance the state machine by one probe; returns events. Pure (no I/O) so it is testable.

    st: power (on|off|unknown), since, last_on (device power-on, epoch s), last_off (first silent
    probe of the last outage), last_seen (last answer), started (when the watch started), plus the
    counters misses / gw_streak and the pre-outage values kept to undo a false "off"
    (prev_since / prev_last_off).

    Events: ("off", {off_at, at_start}), ("on", {boot, off_at}), ("unobserved", {boot}),
    ("false_alarm", {boot}), ("baseline", {boot}).
    """
    out: list[tuple[str, dict]] = []
    if up is not None:
        boot = int(now - up)
        st["misses"] = 0
        st["gw_streak"] += 1  # the device answering means the LAN is up
        st["last_seen"] = int(now)
        if st["power"] == "off" and st["last_on"] == 0:
            # "Off" was declared before the device ever answered (watch started during an outage,
            # or the device was slow to join Wi-Fi): only its boot time tells which.
            if boot >= st["started"] - BOOT_JUMP_S:
                out.append(("on", {"boot": boot, "off_at": None}))
            else:
                out.append(("false_alarm", {"boot": boot}))
            st.update(power="on", since=boot, last_on=boot)
        elif st["power"] == "off":
            if abs(boot - st["last_on"]) <= BOOT_JUMP_S:
                # Same boot as before the "outage": it never lost power, it only dropped off Wi-Fi.
                # (Compared with the known boot, not last_off: a device slow to rejoin Wi-Fi can
                # reboot BEFORE the watch declared "off" and still be a real power return.)
                out.append(("false_alarm", {"boot": boot}))
                st.update(power="on", since=st["prev_since"], last_off=st["prev_last_off"])
            else:
                out.append(("on", {"boot": boot, "off_at": st["last_off"]}))
                st.update(power="on", since=boot, last_on=boot)
        elif st["power"] == "unknown":
            out.append(("baseline", {"boot": boot}))
            st.update(power="on", since=boot, last_on=boot)
        elif boot > st["last_on"] + BOOT_JUMP_S:
            # Rebooted between two answered probes: the outage fell while we could not look
            # (box offline during the router's reboot, or shorter than off_after probes).
            out.append(("unobserved", {"boot": boot}))
            st.update(since=boot, last_on=boot)
    elif gw_ok:
        st["gw_streak"] += 1
        # Count misses only once the router has been back for off_after probes: after a router
        # reboot the device needs a while to rejoin Wi-Fi, and that is not an outage.
        if st["gw_streak"] > off_after:
            st["misses"] += 1
            if st["misses"] == off_after and st["power"] != "off":
                off_at = int(now - (off_after - 1) * interval)
                out.append(("off", {"off_at": off_at, "at_start": st["power"] == "unknown"}))
                st.update(prev_since=st["since"], prev_last_off=st["last_off"])
                st.update(power="off", since=off_at, last_off=off_at)
    else:
        # The box cannot reach its own router: the device's silence says nothing.
        st.update(misses=0, gw_streak=0)
    return out


def describe(kind: str, ev: dict) -> str:
    """One journal line per event (English, UTC)."""
    if kind == "off":
        return f"power OFF — device silent since ~{iso(ev['off_at'])} while the router answers"
    if kind == "on":
        since = f"; silent since ~{iso(ev['off_at'])}" if ev["off_at"] else ""
        return f"power ON — device powered on at {iso(ev['boot'])}{since}"
    if kind == "unobserved":
        return f"power ON — device powered on at {iso(ev['boot'])} (outage not observed: box offline or too short)"
    if kind == "false_alarm":
        return f"device back without a reboot (up since {iso(ev['boot'])}) — Wi-Fi gap, not a power cut"
    return f"power on — device up since {iso(ev['boot'])}"


def fmt_time(ts: float, tz: tzinfo | None, now: float) -> str:
    """HH:MM in tz, with the date when it is not today's."""
    t = datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts).astimezone()
    today = datetime.fromtimestamp(now, t.tzinfo).date()
    return t.strftime("%H:%M") if t.date() == today else t.strftime("%d.%m %H:%M")


def fmt_duration(seconds: float, msgs: dict) -> str:
    minutes = max(1, round(seconds / 60))
    h, m = divmod(minutes, 60)
    return msgs["hm"].format(h=h, m=m) if h else msgs["m"].format(m=m)


def message(kind: str, ev: dict, lang: str, tz: tzinfo | None, now: float, label: str = "") -> str | None:
    """The channel post for an event, or None when the event is not worth one."""
    msgs = MESSAGES[lang]
    if kind == "off":
        text = msgs["off_start" if ev["at_start"] else "off"].format(t=fmt_time(ev["off_at"], tz, now))
    elif kind == "on":
        t = fmt_time(ev["boot"], tz, now)
        # A device that rebooted before "off" was declared (short cut over a router reboot)
        # has no meaningful duration.
        if ev["off_at"] and ev["boot"] - ev["off_at"] >= 60:
            text = msgs["on"].format(t=t, d=fmt_duration(ev["boot"] - ev["off_at"], msgs))
        else:
            text = msgs["on_nodur"].format(t=t)
    elif kind == "unobserved":
        text = msgs["unobserved"].format(t=fmt_time(ev["boot"], tz, now))
    elif kind == "false_alarm":
        text = msgs["false_alarm"]
    else:
        return None
    return f"{label}: {text}" if label else text


class Telegram:
    """Posts in order from a background thread, retrying until Telegram is reachable again
    (the network is often down right when power returns), so probing never blocks on it."""

    def __init__(self, token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.q: queue.Queue[str] = queue.Queue()

    def start(self) -> "Telegram":
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def post(self, text: str) -> None:
        self.q.put(text)

    def send(self, text: str) -> tuple[str, float]:
        """One attempt: ("sent" | "dropped" | "retry", seconds to wait before retrying). A permanent
        rejection (bad token or chat) is logged and dropped rather than retried forever."""
        body = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        try:
            with urllib.request.urlopen(self.url, data=body, timeout=15) as r:
                r.read()
            return "sent", 0
        except urllib.error.HTTPError as e:
            # Never log e.url / the request: the URL carries the bot token.
            try:
                detail = json.loads(e.read() or b"{}")
            except ValueError:
                detail = {}
            if e.code == 429:
                return "retry", float(detail.get("parameters", {}).get("retry_after", 30))
            if 400 <= e.code < 500:
                log(f"telegram: HTTP {e.code} {detail.get('description', '')} — message dropped")
                return "dropped", 0
            log(f"telegram: HTTP {e.code} — will retry")
            return "retry", 30
        except (OSError, ValueError) as e:
            log(f"telegram: {type(e).__name__} — will retry")
            return "retry", 30

    def _run(self) -> None:
        while True:
            text = self.q.get()
            delay = 10.0
            while True:
                status, wait = self.send(text)
                if status != "retry":
                    break
                time.sleep(max(wait, delay))
                delay = min(delay * 2, 120)


def load_state(path: str, st: dict) -> None:
    try:
        with open(path) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    st.update({k: saved[k] for k in PERSIST_KEYS if k in saved})


def write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def config() -> dict:
    lang = os.environ.get("POWER_WATCH_LANG", "en")
    tz_name = os.environ.get("POWER_WATCH_TZ", "")
    tz = None
    if tz_name:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    return {
        "device": os.environ.get("POWER_WATCH_DEVICE_IP", ""),
        "token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "chat": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "lang": lang if lang in MESSAGES else "en",
        "tz": tz,
        "label": os.environ.get("POWER_WATCH_LABEL", ""),
        "gateway": os.environ.get("POWER_WATCH_GATEWAY", ""),
        "gw_port": int(os.environ.get("POWER_WATCH_GATEWAY_PORT", "53")),
        "interval": float(os.environ.get("POWER_WATCH_INTERVAL_S", "15")),
        "off_after": int(os.environ.get("POWER_WATCH_OFF_AFTER", "4")),
    }


def wait_for_clock(limit_s: float = 600) -> None:
    """Boards without an RTC (Raspberry Pi) boot with a stale clock, and every boot time is
    now - uptime: probing before NTP would post a power return that never happened. Waits for
    systemd-timesyncd's sync flag when timesyncd is in use; other setups should order the unit
    after time-sync.target (systemd-time-wait-sync)."""
    if not os.path.isdir("/run/systemd/timesync") or os.path.exists("/run/systemd/timesync/synchronized"):
        return
    log("power-watch: waiting for the clock to sync")
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline and not os.path.exists("/run/systemd/timesync/synchronized"):
        time.sleep(5)


def watch(cfg: dict) -> int:
    if not cfg["device"]:
        log("power-watch: POWER_WATCH_DEVICE_IP unset — nothing to watch")
        return 2  # the unit's RestartPreventExitStatus: a config error, not worth retrying
    wait_for_clock()
    tg = Telegram(cfg["token"], cfg["chat"]).start() if cfg["token"] and cfg["chat"] else None
    interval, off_after = cfg["interval"], cfg["off_after"]
    run_dir, state_dir = os.environ.get("RUNTIME_DIRECTORY", ""), os.environ.get("STATE_DIRECTORY", "")
    persist_path = os.path.join(state_dir, "state.json") if state_dir else ""

    now = int(time.time())
    st = {"power": "unknown", "since": now, "last_on": 0, "last_off": 0, "last_seen": 0, "checked": 0,
          "misses": 0, "gw_streak": 0, "prev_since": now, "prev_last_off": 0, "started": now}
    if persist_path:
        load_state(persist_path, st)
    log(f"power-watch: device {cfg['device']}, gateway {cfg['gateway'] or '(default route)'}:{cfg['gw_port']}, "
        f"every {interval:g}s, off after {off_after} misses, telegram {'on' if tg else 'off'}, "
        f"starting from power={st['power']}")
    while True:
        t0 = time.time()
        up = device_uptime(cfg["device"])
        gw_ok = True
        if up is None:
            # Re-resolved every probe: at boot the default route may not exist yet.
            gateway = cfg["gateway"] or default_gateway()
            gw_ok = bool(gateway) and gateway_up(gateway, cfg["gw_port"])
        events = step(st, up, gw_ok, t0, interval, off_after)
        for kind, ev in events:
            log(describe(kind, ev))
            text = message(kind, ev, cfg["lang"], cfg["tz"], t0, cfg["label"])
            if tg and text:
                tg.post(text)
        if events and persist_path:
            write_atomic(persist_path, json.dumps({k: st[k] for k in PERSIST_KEYS}))
        st["checked"] = int(t0)
        if run_dir:
            write_atomic(os.path.join(run_dir, "state"), "".join(f"{k}={st[k]}\n" for k in PUBLIC_KEYS))
        time.sleep(max(0.0, interval - (time.time() - t0)))


def arp_table() -> dict[str, str]:
    try:
        with open("/proc/net/arp") as f:
            return {l.split()[0]: l.split()[3] for l in f.readlines()[1:] if len(l.split()) > 3}
    except OSError:
        return {}


def scan(cidr: str) -> int:
    """Unicast hello to every host (broadcast replies are often dropped by host firewalls)."""
    net = ipaddress.ip_network(cidr, strict=False)
    if net.num_addresses > 1024:
        log("power-watch: --scan takes at most a /22")
        return 2
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for host in net.hosts():
            try:
                s.sendto(MIIO_HELLO, (str(host), MIIO_PORT))
            except OSError:
                pass
        s.settimeout(0.5)
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                data, (ip, _) = s.recvfrom(1024)
            except OSError:
                continue
            hello = parse_hello(data)
            if hello:
                found[ip] = hello
    arp = arp_table()
    for ip in sorted(found, key=ipaddress.ip_address):
        dev_id, up = found[ip]
        log(f"{ip:15}  mac {arp.get(ip, '?'):17}  id {dev_id:>10}  up since {iso(time.time() - up)}")
    log(f"{len(found)} miIO device(s) found")
    return 0


def probe(ip: str) -> int:
    up = device_uptime(ip)
    if up is None:
        log(f"{ip}: no answer")
        return 1
    log(f"{ip}: up since {iso(time.time() - up)} ({up} s)")
    return 0


def test_notify(cfg: dict) -> int:
    if not (cfg["token"] and cfg["chat"]):
        log("power-watch: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset")
        return 2
    status, _ = Telegram(cfg["token"], cfg["chat"]).send(MESSAGES[cfg["lang"]]["test"])
    log(f"power-watch: test message {status}")
    return 0 if status == "sent" else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Watch mains power through a Wi-Fi device; post outages to Telegram.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--scan", metavar="CIDR", help="list miIO devices on the LAN, e.g. 192.168.0.0/24")
    g.add_argument("--probe", metavar="IP", help="probe one device once")
    g.add_argument("--test-notify", action="store_true", help="send a test message to the Telegram chat")
    args = p.parse_args()
    if args.scan:
        return scan(args.scan)
    if args.probe:
        return probe(args.probe)
    cfg = config()
    if args.test_notify:
        return test_notify(cfg)
    return watch(cfg)


if __name__ == "__main__":
    sys.exit(main())
