"""The power-watch command: the service itself, plus the setup and diagnostic subcommands."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
import time
import urllib.error

from power_watch import __version__, config, probe
from power_watch.messages import MESSAGES, iso
from power_watch.telegram import Telegram, error_detail, fetch_chats
from power_watch.watch import load_purifier, watch

log = logging.getLogger("power_watch")


def cmd_scan(cidr: str) -> int:
    try:
        found = probe.scan(cidr)
    except ValueError as e:
        print(f"power-watch: {e}", file=sys.stderr)
        return 2
    arp = probe.arp_table()
    for ip in sorted(found, key=ipaddress.ip_address):
        dev_id, up = found[ip]
        print(f"{ip:15}  mac {arp.get(ip, '?'):17}  id {dev_id:>10}  up since {iso(time.time() - up)}")
    print(f"{len(found)} miIO device(s) found")
    return 0


def cmd_probe(ip: str) -> int:
    reply = probe.hello(ip)
    if reply is None:
        print(f"{ip}: no answer")
        return 1
    device_id, up = reply
    print(f"{ip}: device {device_id}, up since {iso(time.time() - up)} ({up} s)")
    return 0


def cmd_test_notify(cfg: config.Config) -> int:
    if not (cfg.token and cfg.chat):
        print("power-watch: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset", file=sys.stderr)
        return 2
    status, _ = Telegram(cfg.token, cfg.chat).send(MESSAGES[cfg.lang]["test"])
    print(f"power-watch: test message {status}")
    return 0 if status == "sent" else 1


def cmd_find_chats(cfg: config.Config) -> int:
    if not cfg.token:
        print("power-watch: TELEGRAM_BOT_TOKEN unset", file=sys.stderr)
        return 2
    try:
        chats = fetch_chats(cfg.token)
    except urllib.error.HTTPError as e:  # 409: the service is running (it reads the updates); stop it first
        print(f"telegram: HTTP {e.code} {error_detail(e).get('description', '')}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — never print the exception text: it may hold the URL
        print(f"telegram: {type(e).__name__}", file=sys.stderr)
        return 1
    for chat_id, what in chats.items():
        print(f"{chat_id}  {what}")
    if not chats:
        print("no chats yet: post something in the channel (with the bot already an admin), then run this again")
    return 0


def cmd_purifier(cfg: config.Config) -> int:
    loaded = load_purifier(cfg)
    if not loaded:
        print("power-watch: no purifier configured (or its dependency is missing, see above)", file=sys.stderr)
        return 2
    module, dev = loaded.module, loaded.device
    try:
        values = dev.read()
    except module.UnsupportedModel as e:
        print(f"{cfg.purifier_ip}: model {e.model} has no property map in power_watch/purifier.py yet")
        return 1
    except Exception as e:  # noqa: BLE001 — a diagnostic command reports, never tracebacks
        print(f"{cfg.purifier_ip}: {type(e).__name__}: {e}")
        return 1
    print(f"{cfg.purifier_ip}: {dev.model} {values}")
    for line in module.status_lines(values, cfg.lang):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="power-watch",
        description="Watch mains power through a Wi-Fi device; post outages to Telegram. "
        "Settings come from the environment (see power-watch.env.example).",
    )
    p.add_argument("--version", action="version", version=f"power-watch {__version__}")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--scan", metavar="CIDR", help="list miIO devices on the LAN, e.g. 192.168.0.0/24")
    g.add_argument("--probe", metavar="IP", help="probe one device once")
    g.add_argument("--find-chats", action="store_true", help="list chats the bot was added to, with their ids")
    g.add_argument("--test-notify", action="store_true", help="send a test message to the Telegram chat")
    g.add_argument("--purifier", action="store_true", help="read the configured air purifier once")
    args = p.parse_args(argv)
    # One line per message, no timestamps: journald adds its own.
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout, force=True)
    if args.scan:
        return cmd_scan(args.scan)
    if args.probe:
        return cmd_probe(args.probe)
    try:
        cfg = config.from_env()
    except config.ConfigError as e:
        log.error("power-watch: config error: %s", e)
        return 2
    if args.test_notify:
        return cmd_test_notify(cfg)
    if args.find_chats:
        return cmd_find_chats(cfg)
    if args.purifier:
        return cmd_purifier(cfg)
    return watch(cfg)
