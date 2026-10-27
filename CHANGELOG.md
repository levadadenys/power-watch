# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-10-27

The first release: detecting outages, posting them, and installing with one command.

### Power watch
- Detects outages through a mains-powered Xiaomi Wi-Fi device (token-free miIO hello): silent
  while the router answers means "out"; a new power-on time from its uptime means "back", with the
  exact time even when the network was down at that moment.
- Handles router reboots (a grace before silence counts), Wi-Fi hiccups ("false alarm"
  corrections), clock steps and missing NTP at boot, and the device's clock drift.
- Follows devices by their permanent miIO id when the router gives them a new IP; an unknown device
  at the sensor's address never causes an outage; `POWER_WATCH_DEVICE_ID` re-enrols a replaced
  sensor (#5, #7, #8).
- Persists the state and unsent posts across restarts, writing only on changes.

### Telegram
- Channel posts in English or Ukrainian, queued until Telegram is reachable.
- Private `/status` replies for allowed users, one per chat per batch.
- Optional Xiaomi air purifier readings in `/status` (PM2.5, mode, filter life), over encrypted
  miIO (#1); a worn-out filter (≤5%) says to replace it (#10).

### Install and operations
- `install.sh`: a reproducible single-file build (stdlib zipapp), first-run setup, upgrades that
  keep settings, uninstall (#4).
- systemd unit with `DynamicUser`; a bad setting stops it with exit 2 and the reason in the journal.
- `power-watch --scan / --probe / --find-chats / --test-notify / --purifier` for setup and checks.

### Project
- The `power_watch` package with a pure, fully tested state machine (#4); CI on Python 3.9–3.13
  with ruff, mypy, shellcheck and a reproducible-build check (#2); README with diagrams and FAQ (#6);
  CONTRIBUTING, SECURITY, issue forms and a PR template (#9).

[0.1.0]: https://github.com/levadadenys/power-watch/releases/tag/v0.1.0
