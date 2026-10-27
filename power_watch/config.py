"""Settings from the environment (systemd's EnvironmentFile, /etc/power-watch.env), validated once
at startup: a bad value stops the service with exit 2 and a message naming it, instead of a
crash loop. See power-watch.env.example for every variable."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import tzinfo

from power_watch.messages import MESSAGES


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    device: str = ""  # the mains-powered miIO device (required to watch)
    token: str = ""  # Telegram bot token
    chat: str = ""  # channel for outage posts: @name or -100… id
    allowed: frozenset[int] = frozenset()  # user ids the bot answers /status for
    lang: str = "en"
    tz: tzinfo | None = None  # None: the system's local time
    label: str = ""  # prefix for every post
    purifier_ip: str = ""
    purifier_token: str = ""
    gateway: str = ""  # "" = the default route's gateway
    gw_port: int = 53
    interval: float = 15
    off_after: int = 4


def from_env(env: Mapping[str, str] | None = None) -> Config:
    environ: Mapping[str, str] = os.environ if env is None else env

    def get(name: str, default: str = "") -> str:
        return environ.get(name, "").strip() or default

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
    purifier_token = get("POWER_WATCH_PURIFIER_TOKEN").lower()
    if purifier_token and (len(purifier_token) != 32 or any(c not in "0123456789abcdef" for c in purifier_token)):
        raise ConfigError("POWER_WATCH_PURIFIER_TOKEN must be 32 hex digits")
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
    return Config(
        device=ip("POWER_WATCH_DEVICE_IP"),
        token=get("TELEGRAM_BOT_TOKEN"),
        chat=get("TELEGRAM_CHAT_ID"),
        allowed=allowed,
        lang=lang,
        tz=tz,
        label=get("POWER_WATCH_LABEL"),
        purifier_ip=ip("POWER_WATCH_PURIFIER_IP"),
        purifier_token=purifier_token,
        gateway=ip("POWER_WATCH_GATEWAY"),
        gw_port=num("POWER_WATCH_GATEWAY_PORT", "53", int, 1),
        interval=num("POWER_WATCH_INTERVAL_S", "15", float, 1),
        off_after=num("POWER_WATCH_OFF_AFTER", "4", int, 1),
    )
