"""The outage state machine: one probe result in, events out. Pure, so every edge case is a test.

The watch keeps a small state dict:
    power          on | off | unknown
    since          when the current state began (epoch s)
    last_on        the device's last power-on time (now - uptime)
    last_off       the first silent probe of the last outage
    last_seen      the device's last answer
    started        when this run of the watch started
    misses         consecutive silent probes while the router answers
    gw_streak      consecutive probes with the LAN up (the grace after a router outage)
    gw_down        consecutive failed router probes
    prev_since, prev_last_off   the values before the last "off", to undo a false one
"""

from __future__ import annotations

from datetime import datetime, timezone

# A real power cut reboots the device; a boot time that moved by more than this since the
# previous answer is a power return. Within it, the same boot (uptime and clock jitter; the
# known boot time follows the device's slow clock drift answer by answer).
BOOT_JUMP_S = 30
# A wall-clock jump larger than this between two probes (NTP stepping the clock) shifts every
# stored time by the same amount instead of being read as a reboot.
CLOCK_STEP_S = 5
TIME_KEYS = ("since", "last_on", "last_off", "last_seen", "checked", "prev_since", "prev_last_off", "started")

Event = tuple[str, dict]


def new_state(now: int) -> dict:
    return {
        "power": "unknown",
        "since": now,
        "last_on": 0,
        "last_off": 0,
        "last_seen": 0,
        "checked": 0,
        "misses": 0,
        "gw_streak": 0,
        "gw_down": 0,
        "prev_since": now,
        "prev_last_off": 0,
        "started": now,
    }


def step(st: dict, up: int | None, gw_ok: bool, now: float, interval: float, off_after: int) -> list[Event]:
    """Advance the state machine by one probe: `up` is the device's uptime (None: no answer),
    `gw_ok` whether the router answered. Returns the events:

        ("off", {off_at, at_start})   power went out (at_start: before the device was ever seen)
        ("on", {boot, off_at})        power is back (off_at None: the start is unknown)
        ("unobserved", {boot})        the device rebooted between two answers
        ("false_alarm", {boot})       an "off" ended without a reboot: only a Wi-Fi gap
        ("baseline", {boot})          first answer of this run; not worth a post
    """
    out: list[Event] = []
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


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
