"""The two state files.

$STATE_DIRECTORY/state.json  persisted: written on transitions only (SD cards), with the unsent
                             posts, so a restart neither repeats nor loses an outage.
$RUNTIME_DIRECTORY/state     live: key=value lines rewritten every probe (tmpfs), a stable contract
                             for other local readers (e.g. an alert script asking whether a network
                             drop was a power return).
"""

from __future__ import annotations

import json
import os

PUBLIC_KEYS = ("power", "since", "last_on", "last_off", "last_seen", "checked")
PERSIST_KEYS = (
    "power",
    "since",
    "last_on",
    "last_off",
    "last_seen",
    "prev_since",
    "prev_last_off",
    "device_id",
    "device_cfg",
)
STR_KEYS = ("device_cfg",)


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
        if k == "power":
            ok = v in ("on", "off", "unknown")
        else:
            ok = isinstance(v, str) if k in STR_KEYS else type(v) is int
        if ok:
            st[k] = v
    outbox = saved.get("outbox")
    return [m for m in outbox if isinstance(m, str)] if isinstance(outbox, list) else []


def save_state(path: str, st: dict, outbox: list[str]) -> None:
    write_atomic(path, json.dumps({**{k: st[k] for k in PERSIST_KEYS}, "outbox": outbox}))


def write_live(path: str, st: dict) -> None:
    write_atomic(path, "".join(f"{k}={st[k]}\n" for k in PUBLIC_KEYS))


def write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())  # this box's whole subject is power loss
    os.replace(tmp, path)
