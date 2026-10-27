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

Stdlib only, Python 3.9+. Config (environment; see power-watch.env.example):
  POWER_WATCH_DEVICE_IP     miIO device on mains power, NOT on the UPS (required)
  TELEGRAM_BOT_TOKEN        bot that posts to the channel (optional: without it,
  TELEGRAM_CHAT_ID          the journal is the only output); chat id or @channel
  POWER_WATCH_LANG          message language: en (default) or uk
  POWER_WATCH_ALLOWED_USERS Telegram user ids (comma-separated) the bot answers /status for in a
                            private chat; others get no reply (their id is logged to add them)
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
  power-watch --find-chats      list the chats the bot was added to (the id of a private channel)
  power-watch --test-notify     send a test message to the Telegram chat
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, tzinfo
from typing import Callable

MIIO_PORT = 54321
# miIO hello: magic 0x2131, length 32, then 28 bytes of 0xff.
MIIO_HELLO = bytes.fromhex("21310020" + "ff" * 28)
# A real power cut reboots the device; a boot time that moved by more than this since the
# previous answer is a power return. Within it, the same boot (uptime and clock jitter; the
# known boot time follows the device's slow clock drift answer by answer).
BOOT_JUMP_S = 30
# A wall-clock jump larger than this between two probes (NTP stepping the clock) shifts every
# stored time by the same amount instead of being read as a reboot.
CLOCK_STEP_S = 5

MESSAGES = {
    "en": {
        "off": "⚡ Power is out since {t}.",
        "off_start": "⚡ Power is out (noticed at {t}).",
        "on": "✅ Power is back at {t}, after {d}.",
        "on_nodur": "✅ Power is back at {t}.",
        "unobserved": "✅ Power came back at {t} after a short or unobserved outage.",
        "false_alarm": "↩️ False alarm: power never went out (the sensor only dropped off Wi-Fi).",
        "test": "🔔 power-watch test message.",
        "status_on": "💡 Power is on since {t} ({d}).",
        "status_off": "⚡ Power is out since {t} ({d}).",
        "status_unknown": "❔ Not known yet: the sensor hasn't answered since the watch started.",
        "help": "Send /status to see whether the power is on.",
        "cmd_status": "Is the power on?",
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
        "status_on": "💡 Світло є з {t} ({d}).",
        "status_off": "⚡ Світла немає з {t} ({d}).",
        "status_unknown": "❔ Поки невідомо: датчик ще не відповідав.",
        "help": "Надішліть /status, щоб дізнатися, чи є світло.",
        "cmd_status": "Чи є світло?",
        "hm": "{h} год {m} хв",
        "m": "{m} хв",
    },
}

# The runtime state file's keys (key=value lines in $RUNTIME_DIRECTORY/state, rewritten every
# probe): a stable contract for other local readers, e.g. an alert script that wants to know
# whether a network drop was a power return.
PUBLIC_KEYS = ("power", "since", "last_on", "last_off", "last_seen", "checked")
# Kept across restarts in $STATE_DIRECTORY/state.json (written on transitions only, to spare
# SD cards) together with the unsent posts, so a restart neither repeats nor loses an outage.
PERSIST_KEYS = ("power", "since", "last_on", "last_off", "last_seen", "prev_since", "prev_last_off")
TIME_KEYS = ("since", "last_on", "last_off", "last_seen", "checked", "prev_since", "prev_last_off", "started")


class ConfigError(ValueError):
    pass


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
    counters misses / gw_streak / gw_down and the pre-outage values kept to undo a false "off"
    (prev_since / prev_last_off).

    Events: ("off", {off_at, at_start}), ("on", {boot, off_at}), ("unobserved", {boot}),
    ("false_alarm", {boot}), ("baseline", {boot}).
    """
    out: list[tuple[str, dict]] = []
    if up is not None:
        boot = int(now - up)
        st["misses"] = st["gw_down"] = 0
        st["gw_streak"] += 1  # the device answering means the LAN is up
        st["last_seen"] = int(now)
        if st["power"] == "off" and st["last_on"] == 0:
            # "Off" was declared before the device ever answered (watch started during an outage,
            # or the device was slow to join Wi-Fi): only its boot time tells which. (min: after a
            # restart, the outage began before this run did.)
            if boot >= min(st["started"], st["last_off"]) - BOOT_JUMP_S:
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
                st.update(power="on", since=st["prev_since"], last_off=st["prev_last_off"], last_on=boot)
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
        else:
            # Same boot: follow the device clock's drift (~1 s/day is common), so it never adds
            # up to a jump.
            st["last_on"] = boot
    elif gw_ok:
        st["gw_down"] = 0
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
        # The box cannot reach its own router: the device's silence says nothing. One failed
        # router probe is skipped; two in a row (a router reboot) restart the count and the grace.
        st["gw_down"] += 1
        if st["gw_down"] >= 2:
            st.update(misses=0, gw_streak=0)
    return out


def shift_times(st: dict, delta: float) -> None:
    """Re-express stored times after the wall clock stepped by delta seconds."""
    for k in TIME_KEYS:
        if st.get(k):
            st[k] = int(st[k] + delta)


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
    (the network is often down right when power returns), so probing never blocks on it.
    The outbox is persisted by the caller (pending()) and after every send (on_sent)."""

    def __init__(self, token: str, chat_id: str, outbox: list[str] | None = None,
                 on_sent: Callable[[], None] | None = None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.url = self.base + "sendMessage"
        self.chat_id = chat_id
        self.outbox = list(outbox or [])
        self.cv = threading.Condition()
        self.on_sent = on_sent or (lambda: None)

    def start(self) -> Telegram:
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def post(self, text: str) -> None:
        with self.cv:
            self.outbox.append(text)
            self.cv.notify()

    def pending(self) -> list[str]:
        with self.cv:
            return list(self.outbox)

    def send(self, text: str, chat_id: str | int | None = None) -> tuple[str, float]:
        """One attempt: ("sent" | "dropped" | "retry", seconds to wait before retrying). A permanent
        rejection (bad token or chat) is logged and dropped rather than retried forever. chat_id
        defaults to the channel."""
        body = urllib.parse.urlencode({"chat_id": chat_id or self.chat_id, "text": text}).encode()
        try:
            with urllib.request.urlopen(self.url, data=body, timeout=15) as r:
                r.read()
            return "sent", 0
        except urllib.error.HTTPError as e:
            # Never log e.url / the request: the URL carries the bot token.
            detail = error_detail(e)
            if e.code == 429:
                try:
                    return "retry", float(detail.get("parameters", {}).get("retry_after", 30))
                except (AttributeError, TypeError, ValueError):
                    return "retry", 30
            if 400 <= e.code < 500:
                log(f"telegram: HTTP {e.code} {detail.get('description', '')} — message dropped")
                return "dropped", 0
            log(f"telegram: HTTP {e.code} — will retry")
            return "retry", 30
        except Exception as e:  # noqa: BLE001 — anything else (garbage replies too) is transient
            log(f"telegram: {type(e).__name__} — will retry")
            return "retry", 30

    def _run(self) -> None:
        while True:
            try:
                with self.cv:
                    while not self.outbox:
                        self.cv.wait()
                    text = self.outbox[0]
                delay = 10.0
                while True:
                    status, wait = self.send(text)
                    if status != "retry":
                        break
                    time.sleep(max(wait, delay))
                    delay = min(delay * 2, 120)
                with self.cv:
                    self.outbox.pop(0)
                self.on_sent()
            except Exception as e:  # noqa: BLE001 — the worker must outlive any surprise
                log(f"telegram: worker {type(e).__name__} — retrying in 30 s")
                time.sleep(30)


def status_text(st: dict, lang: str, tz: tzinfo | None, now: float) -> str:
    msgs = MESSAGES[lang]
    if st["power"] not in ("on", "off"):
        return msgs["status_unknown"]
    return msgs[f"status_{st['power']}"].format(t=fmt_time(st["since"], tz, now),
                                                 d=fmt_duration(now - st["since"], msgs))


STATUS_WORDS = ("/status", "/start", "?", "status", "статус", "світло")


def reply_for(update: dict, allowed: frozenset[int], status: Callable[[], str], lang: str) -> tuple[int, str] | str | None:
    """What to do with one getUpdates entry: (chat id, reply) for an allowed user's private
    message, a log line for anyone else's, None for the rest (channel posts, edits, ...)."""
    msg = update.get("message")
    if not isinstance(msg, dict) or (msg.get("chat") or {}).get("type") != "private":
        return None
    user = msg.get("from") or {}
    if user.get("id") not in allowed:
        return (f"bot: ignored a message from user {user.get('id')} (@{user.get('username', '?')}) — "
                f"add the id to POWER_WATCH_ALLOWED_USERS to let them ask")
    text = (msg.get("text") or "").strip().lower()
    command = text.split()[0].split("@")[0] if text else ""
    if command in STATUS_WORDS or text in STATUS_WORDS:
        return msg["chat"]["id"], status()
    return msg["chat"]["id"], MESSAGES[lang]["help"]


class Commands:
    """Answers private /status messages: long-polls getUpdates (the one consumer of the bot's
    updates — a second one, even --find-chats, gets HTTP 409 while this runs)."""

    def __init__(self, tg: Telegram, allowed: frozenset[int], status: Callable[[], str], lang: str):
        self.tg, self.allowed, self.status, self.lang = tg, allowed, status, lang

    def start(self) -> Commands:
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def call(self, method: str, params: dict, timeout: float) -> dict:
        body = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (list, dict)) else v
                                       for k, v in params.items()}).encode()
        with urllib.request.urlopen(self.tg.base + method, data=body, timeout=timeout) as r:
            return json.loads(r.read())

    def _run(self) -> None:
        try:  # the "/" menu in the chat; cosmetic, so one try
            self.call("setMyCommands", {"commands": [{"command": "status",
                                                      "description": MESSAGES[self.lang]["cmd_status"]}]}, 15)
        except Exception as e:  # noqa: BLE001
            log(f"bot: setMyCommands {type(e).__name__}")
        offset = 0
        while True:
            try:
                reply = self.call("getUpdates", {"offset": offset, "timeout": 50,
                                                 "allowed_updates": ["message"]}, 60)
                for update in reply.get("result", []):
                    offset = max(offset, update.get("update_id", 0) + 1)
                    action = reply_for(update, self.allowed, self.status, self.lang)
                    if isinstance(action, str):
                        log(action)
                    elif action:
                        self.tg.send(action[1], action[0])
            except urllib.error.HTTPError as e:
                # Never log e.url: it carries the token. 409 = another getUpdates consumer.
                log(f"bot: getUpdates HTTP {e.code} {error_detail(e).get('description', '')}")
                time.sleep(30)
            except Exception as e:  # noqa: BLE001 — network down etc.; keep listening
                log(f"bot: getUpdates {type(e).__name__}")
                time.sleep(15)


def error_detail(e: urllib.error.HTTPError) -> dict:
    """Telegram's JSON error body as a dict; {} for anything else (empty, truncated, not JSON)."""
    try:
        detail = json.loads(e.read() or b"{}")
    except Exception:  # noqa: BLE001
        return {}
    return detail if isinstance(detail, dict) else {}


def load_state(path: str, st: dict) -> list[str]:
    """Restore st from state.json (keys with the wrong type are ignored); returns unsent posts."""
    try:
        with open(path) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(saved, dict):
        return []
    for k in PERSIST_KEYS:
        v = saved.get(k)
        if (v in ("on", "off", "unknown")) if k == "power" else (type(v) is int):
            st[k] = v
    outbox = saved.get("outbox")
    return [m for m in outbox if isinstance(m, str)] if isinstance(outbox, list) else []


def write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())  # this box's whole subject is power loss
    os.replace(tmp, path)


def config(env: dict | None = None) -> dict:
    """Validated settings; raises ConfigError naming the bad variable (the unit then stops with
    exit 2 instead of crash-looping)."""
    env = os.environ if env is None else env

    def get(name: str, default: str = "") -> str:
        return env.get(name, "").strip() or default

    def num(name: str, default: str, cast: Callable, minimum: float):
        try:
            v = cast(get(name, default))
        except ValueError:
            raise ConfigError(f"{name}={get(name)!r} is not a number (comments must be on their own line)") from None
        if v < minimum:
            raise ConfigError(f"{name} must be at least {minimum}")
        return v

    def ip(name: str) -> str:
        v = get(name)
        try:
            return str(ipaddress.IPv4Address(v)) if v else ""
        except ValueError:
            raise ConfigError(f"{name}={v!r} is not an IPv4 address") from None

    try:
        allowed = frozenset(int(u) for u in get("POWER_WATCH_ALLOWED_USERS").replace(",", " ").split())
    except ValueError:
        raise ConfigError("POWER_WATCH_ALLOWED_USERS must be numeric Telegram user ids, comma-separated") from None
    lang = get("POWER_WATCH_LANG", "en")
    if lang not in MESSAGES:
        raise ConfigError(f"POWER_WATCH_LANG={lang!r}: one of {', '.join(MESSAGES)}")
    tz = None
    if get("POWER_WATCH_TZ"):
        from zoneinfo import ZoneInfo
        try:
            tz = ZoneInfo(get("POWER_WATCH_TZ"))
        except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
            raise ConfigError(f"POWER_WATCH_TZ={get('POWER_WATCH_TZ')!r} is not a known time zone") from None
    return {
        "device": ip("POWER_WATCH_DEVICE_IP"),
        "token": get("TELEGRAM_BOT_TOKEN"),
        "chat": get("TELEGRAM_CHAT_ID"),
        "lang": lang,
        "tz": tz,
        "label": get("POWER_WATCH_LABEL"),
        "allowed": allowed,
        "gateway": ip("POWER_WATCH_GATEWAY"),
        "gw_port": num("POWER_WATCH_GATEWAY_PORT", "53", int, 1),
        "interval": num("POWER_WATCH_INTERVAL_S", "15", float, 1),
        "off_after": num("POWER_WATCH_OFF_AFTER", "4", int, 1),
    }


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
            log("power-watch: waiting for the clock to sync")
        time.sleep(5)
        waited += 5


def watch(cfg: dict) -> int:
    if not cfg["device"]:
        log("power-watch: POWER_WATCH_DEVICE_IP unset — nothing to watch")
        return 2  # the unit's RestartPreventExitStatus: a config error, not worth retrying
    wait_for_clock()
    interval, off_after = cfg["interval"], cfg["off_after"]
    run_dir, state_dir = os.environ.get("RUNTIME_DIRECTORY", ""), os.environ.get("STATE_DIRECTORY", "")
    persist_path = os.path.join(state_dir, "state.json") if state_dir else ""

    now = int(time.time())
    st = {"power": "unknown", "since": now, "last_on": 0, "last_off": 0, "last_seen": 0, "checked": 0,
          "misses": 0, "gw_streak": 0, "gw_down": 0, "prev_since": now, "prev_last_off": 0, "started": now}
    outbox = load_state(persist_path, st) if persist_path else []
    lock = threading.Lock()
    tg: Telegram | None = None

    def persist() -> None:
        if not persist_path:
            return
        data = {k: st[k] for k in PERSIST_KEYS}
        if tg:
            data["outbox"] = tg.pending()
        with lock:
            try:
                write_atomic(persist_path, json.dumps(data))
            except OSError as e:  # keep watching: dying here would re-post the same event on restart
                log(f"power-watch: cannot save state: {e}")

    if cfg["token"] and cfg["chat"]:
        tg = Telegram(cfg["token"], cfg["chat"], outbox, on_sent=persist).start()
    if cfg["token"]:
        # Listen even with nobody allowed yet: an unknown user's message logs the id to allow.
        Commands(tg or Telegram(cfg["token"], ""), cfg["allowed"],
                 lambda: status_text(st, cfg["lang"], cfg["tz"], time.time()), cfg["lang"]).start()
    log(f"power-watch: device {cfg['device']}, gateway {cfg['gateway'] or '(default route)'}:{cfg['gw_port']}, "
        f"every {interval:g}s, off after {off_after} misses, telegram {'on' if tg else 'off'}, "
        f"{len(cfg['allowed'])} bot user(s), "
        f"starting from power={st['power']}" + (f", {len(outbox)} unsent post(s)" if outbox else ""))
    clock_offset = time.time() - time.monotonic()
    while True:
        t0, m0 = time.time(), time.monotonic()
        step_s = (t0 - m0) - clock_offset
        clock_offset = t0 - m0
        if abs(step_s) > CLOCK_STEP_S:
            log(f"power-watch: wall clock stepped {step_s:+.0f} s — shifting stored times")
            shift_times(st, step_s)
            persist()
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
        if events:
            persist()
        st["checked"] = int(t0)
        if run_dir:
            try:
                write_atomic(os.path.join(run_dir, "state"), "".join(f"{k}={st[k]}\n" for k in PUBLIC_KEYS))
            except OSError as e:
                log(f"power-watch: cannot write {run_dir}/state: {e}")
        time.sleep(max(0.0, interval - (time.monotonic() - m0)))


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


def chats_from_updates(updates: dict) -> dict[int, str]:
    """{chat id: "type title"} for every chat in a getUpdates reply."""
    chats = {}
    for u in updates.get("result", []):
        for kind in ("my_chat_member", "channel_post", "message", "chat_member"):
            chat = (u.get(kind) or {}).get("chat")
            if chat and "id" in chat:
                chats[chat["id"]] = f"{chat.get('type', '?')} {chat.get('title') or chat.get('username') or ''}".strip()
    return chats


def find_chats(cfg: dict) -> int:
    if not cfg["token"]:
        log("power-watch: TELEGRAM_BOT_TOKEN unset")
        return 2
    url = f"https://api.telegram.org/bot{cfg['token']}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            chats = chats_from_updates(json.loads(r.read()))
    except urllib.error.HTTPError as e:
        log(f"telegram: HTTP {e.code} {error_detail(e).get('description', '')}")
        return 1
    except Exception as e:  # noqa: BLE001
        log(f"telegram: {type(e).__name__}")
        return 1
    for chat_id, what in chats.items():
        log(f"{chat_id}  {what}")
    if not chats:
        log("no chats yet: post something in the channel (with the bot already an admin), then run this again")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Watch mains power through a Wi-Fi device; post outages to Telegram.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--scan", metavar="CIDR", help="list miIO devices on the LAN, e.g. 192.168.0.0/24")
    g.add_argument("--probe", metavar="IP", help="probe one device once")
    g.add_argument("--find-chats", action="store_true", help="list chats the bot was added to, with their ids")
    g.add_argument("--test-notify", action="store_true", help="send a test message to the Telegram chat")
    args = p.parse_args()
    if args.scan:
        return scan(args.scan)
    if args.probe:
        return probe(args.probe)
    try:
        cfg = config()
    except ConfigError as e:
        log(f"power-watch: config error: {e}")
        return 2
    if args.test_notify:
        return test_notify(cfg)
    if args.find_chats:
        return find_chats(cfg)
    return watch(cfg)


if __name__ == "__main__":
    sys.exit(main())
