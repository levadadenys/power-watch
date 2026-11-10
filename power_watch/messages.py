"""What people read: channel posts and /status replies, in each supported language. Pure.

To add a language, add a block to MESSAGES with every key (a test checks that).
"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo

MESSAGES = {
    "en": {
        "off": "⚡ Power is out since {t}.",
        "off_dur": "⚡ Power is out since {t}. It was on for {d}.",
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
        "dh": "{d} d {h} h",
        "hm": "{h} h {m} min",
        "m": "{m} min",
        "stale": "The sensor hasn't answered for {d}.",
    },
    "uk": {
        "off": "⚡ Світла немає з {t}.",
        "off_dur": "⚡ Світла немає з {t}. Було {d}.",
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
        "dh": "{d} д {h} год",
        "hm": "{h} год {m} хв",
        "m": "{m} хв",
        "stale": "Датчик не відповідає вже {d}.",
    },
}


# Shorter than this, a duration means nothing (a reboot or a blip): the post leaves it out.
MIN_SPAN_S = 60


def iso(ts: float) -> str:
    """UTC timestamp for journal lines and CLI output."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_time(ts: float, tz: tzinfo | None, now: float) -> str:
    """HH:MM in tz (default: the system's zone), with the date when it is not today's."""
    t = datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts).astimezone()
    today = datetime.fromtimestamp(now, t.tzinfo).date()
    return t.strftime("%H:%M") if t.date() == today else t.strftime("%d.%m %H:%M")


def fmt_duration(seconds: float, msgs: dict) -> str:
    minutes = max(1, round(seconds / 60))
    h, m = divmod(minutes, 60)
    if h >= 24:
        return msgs["dh"].format(d=h // 24, h=h % 24)
    return msgs["hm"].format(h=h, m=m) if h else msgs["m"].format(m=m)


def message(kind: str, ev: dict, lang: str, tz: tzinfo | None, now: float, label: str = "") -> str | None:
    """The channel post for a state-machine event, or None when the event is not worth one."""
    msgs = MESSAGES[lang]
    if kind == "off":
        t = fmt_time(ev["off_at"], tz, now)
        on_since = ev["on_since"]
        if ev["at_start"]:
            text = msgs["off_start"].format(t=t)
        elif on_since and ev["off_at"] - on_since >= MIN_SPAN_S:
            text = msgs["off_dur"].format(t=t, d=fmt_duration(ev["off_at"] - on_since, msgs))
        else:
            text = msgs["off"].format(t=t)
    elif kind == "on":
        t = fmt_time(ev["boot"], tz, now)
        # A device that rebooted before "off" was declared (short cut over a router reboot)
        # has no meaningful duration.
        if ev["off_at"] and ev["boot"] - ev["off_at"] >= MIN_SPAN_S:
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


def status_text(st: dict, lang: str, tz: tzinfo | None, now: float, stale_after: float = 60) -> str:
    """The power line of a /status reply."""
    msgs, s = MESSAGES[lang], dict(st)  # one snapshot: the main thread keeps updating st
    if s["power"] not in ("on", "off"):
        return msgs["status_unknown"]
    text = msgs[f"status_{s['power']}"].format(t=fmt_time(s["since"], tz, now), d=fmt_duration(now - s["since"], msgs))
    # Silent but not (yet) declared off: say so rather than state "on" with confidence.
    if s["power"] == "on" and s.get("last_seen") and now - s["last_seen"] > stale_after:
        text += " " + msgs["stale"].format(d=fmt_duration(now - s["last_seen"], msgs))
    return text
