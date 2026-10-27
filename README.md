# power-watch

Know when the power goes out at home, and tell a Telegram channel.

```
⚡ Power is out since 14:02.
✅ Power is back at 16:40, after 2 h 38 min.
```

Built for places with regular power outages, where the router and a small always-on
box (a Raspberry Pi) sit on a UPS. Because they're on the UPS, they never notice an outage.
A cheap Xiaomi Wi-Fi device plugged straight into the wall (a desk lamp, a smart plug, an air
purifier) does, and it answers a simple network "hello" while it has power. power-watch asks it every
15 seconds.

Plain Python (standard library only), no cloud, no device token. The optional air purifier readings need `python3-cryptography`.

## How it works

- **Probe.** Xiaomi Wi-Fi devices speak the miIO protocol. A 32-byte "hello" to UDP port 54321
  gets a reply without any pairing or token. The reply carries the device's **uptime**, so
  `now − uptime` is the exact moment it last powered on.
- **Outage.** The device stops answering (4 probes in a row, about a minute) while the router still
  answers. If the router itself is unreachable, the device's silence means nothing and is ignored.
  After the router has been unreachable, or right after a start, the count begins only once the
  router has answered for a minute, so detection then takes about 2 minutes.
- **Return.** The device answers again with a new power-on time. That time is the moment power came
  back, even if the network was down at that moment. Many routers reboot when mains returns and
  the UPS switches back, so the "power is back" post usually goes out a couple of minutes late
  with the correct time.
- **No false alarms from Wi-Fi.** If the device comes back *without* having rebooted, power never
  went out, and the channel gets a correction. After the router reboots, the device gets a minute
  of grace to rejoin Wi-Fi before its silence counts.
- **Restarts.** The power state and any posts not yet sent survive restarts
  (`/var/lib/power-watch/state.json`, written only on changes), so a restart neither repeats nor
  loses an outage.
- **Clock.** Times come from the box's clock, so it waits for NTP after a boot (a Raspberry Pi has
  no clock battery), and if the clock is stepped later, the stored times are shifted with it.

## You need

- An always-on Linux box with systemd and Python 3.9+, on the same LAN as the device. Put it and the
  router on a UPS (otherwise there is nobody left to post during an outage).
- A Xiaomi / Mi Home Wi-Fi device on **mains** power, NOT on the UPS. Anything that works with the Mi
  Home app over Wi-Fi usually speaks miIO. Bluetooth and Zigbee devices don't.
- A Telegram bot and a channel (below), unless you only want the journal.

## Setup

**1. Get the code onto the box**

```sh
git clone https://github.com/levadadenys/power-watch && cd power-watch
python3 -m unittest discover -s tests   # optional
```

**2. Find the device**

```sh
python3 power_watch.py --scan 192.168.0.0/24
# 192.168.0.150    mac 78:11:dc:xx:xx:xx  id  123456789  up since 2026-10-27T08:15:02Z
```

To be sure which one it is, unplug it and scan again. In the router's settings, reserve that
address for its MAC (DHCP reservation / static lease) so it never changes. Check a single device
with `--probe 192.168.0.150`.

Also check that the router answers the TCP port power-watch probes (53 by default). A refusal is
fine; a timeout is not:

```sh
nc -vz 192.168.0.1 53     # "succeeded" or "refused" = good; set POWER_WATCH_GATEWAY_PORT otherwise
```

**3. Telegram**

1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` and copy the token.
2. Create a channel. Make it public with an @name, or private and share an invite link.
3. Channel settings → Administrators → add your bot, with permission to post messages.
4. The chat id is `@channelname` for a public channel. For a private one, post anything in the
   channel after adding the bot, then run `power-watch --find-chats` with the token set (step 5
   shows how to run it with the env file; stop the service first if it's running) and copy the
   `-100…` id.

**4. Install**

```sh
sudo install -d /opt/power-watch && sudo install -m 644 purifier.py /opt/power-watch/
sudo install -m 755 power_watch.py /opt/power-watch/
sudo ln -sf /opt/power-watch/power_watch.py /usr/local/bin/power-watch
sudo install -m 644 systemd/power-watch.service /etc/systemd/system/
sudo install -m 600 power-watch.env.example /etc/power-watch.env
sudoedit /etc/power-watch.env          # device IP, token, chat id, language
sudo systemctl daemon-reload && sudo systemctl enable --now power-watch.service
```

**Upgrading:** run only the first three lines (they also replace an older single-file
`/usr/local/bin/power-watch` with the link), then `sudo systemctl restart power-watch`. Don't
rerun the `power-watch.env.example` line: it would overwrite your settings.

**5. Check**

```sh
# a test post in the channel, with the env file read exactly as the service reads it:
sudo systemd-run --pipe --wait -q -p EnvironmentFile=/etc/power-watch.env -p DynamicUser=yes \
  /usr/bin/python3 /usr/local/bin/power-watch --test-notify
journalctl -u power-watch -f       # one line per change; config errors show here too
cat /run/power-watch/state         # live state
```

Then unplug the device: after about a minute the channel says the power is out, and when you
plug it back in it says the power is back.

## Ask the bot

In a private chat with the bot, `/status` (or `?`) answers with the current state:

```
💡 Power is on since 11:23 (2 h 5 min).
```

With an air purifier configured, the reply also has its readings:

```
💡 Power is on since 11:23 (2 h 5 min).
🌫 PM2.5: 7 µg/m³ (good)
Purifier: auto
🧻 Filter: 63% (~120 days left)
```

It answers only the Telegram users in `POWER_WATCH_ALLOWED_USERS`. To add yourself, message the
bot once, find your id in `journalctl -u power-watch` ("ignored a message from user 123…"), put it
in the env file and restart the service. The service is the bot's only reader of its updates, so
stop it before running `--find-chats`.

### Air purifier (optional)

Reading a Xiaomi purifier needs more than the power probe does: its **token** (32 hex digits;
[Xiaomi Cloud Tokens Extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)
prints it after a Mi account login) and `python3-cryptography` (`sudo apt install
python3-cryptography`). Set `POWER_WATCH_PURIFIER_IP` and `POWER_WATCH_PURIFIER_TOKEN`, then check
with `power-watch --purifier` (run it like the test post in step 5). Re-adding the device in Mi Home
changes its token.

Supported models (property maps in `purifier.py`, from [miot-spec](https://home.miot-spec.com)):

| Model | Readings |
|---|---|
| `xiaomi.airp.cpa4` (Smart Air Purifier 4 Compact) | PM2.5, mode, filter life (% and days), faults |

Adding another MiOT purifier means a `MODELS` entry with its own siid/piid numbers, checked
against its spec on miot-spec. Its mode and fault values may differ from the 4 Compact's too.
Humidity and temperature are shown when the map includes them. PRs welcome.

`/status` waits at most about 5 seconds for the purifier. If it doesn't answer in time, the reply
says so, and the reason is in `journalctl -u power-watch` (for example a wrong token or no
readings). A broken or missing `python3-cryptography` turns the readings off; it never stops the
power watch.

## Configuration

`/etc/power-watch.env` (see [`power-watch.env.example`](power-watch.env.example)):

| Variable | Default | |
|---|---|---|
| `POWER_WATCH_DEVICE_IP` | — | the miIO device on mains (required) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | without them, only the journal |
| `POWER_WATCH_ALLOWED_USERS` | empty | Telegram user ids the bot answers `/status` for |
| `POWER_WATCH_PURIFIER_IP`, `POWER_WATCH_PURIFIER_TOKEN` | empty | optional air purifier for `/status` |
| `POWER_WATCH_LANG` | `en` | `en` or `uk` |
| `POWER_WATCH_TZ` | system local time | IANA zone for times in messages, e.g. `Europe/Kyiv` |
| `POWER_WATCH_LABEL` | empty | prefix for every message, e.g. a place name |
| `POWER_WATCH_GATEWAY` | default route | the router's IP |
| `POWER_WATCH_GATEWAY_PORT` | `53` | TCP port probed on the router. A refused connection counts as "up" |
| `POWER_WATCH_INTERVAL_S` | `15` | seconds between probes |
| `POWER_WATCH_OFF_AFTER` | `4` | missed probes before "off", and the grace after a router outage |

Messages are in English or Ukrainian. To add a language, add a block to `MESSAGES` in
`power_watch.py` (the tests check that every language has every key).

## Live state for other tools

`/run/power-watch/state` is rewritten on every probe as `key=value` lines. These keys are stable:

| Key | |
|---|---|
| `power` | `on`, `off` or `unknown` |
| `since` | when the current state began (epoch seconds) |
| `last_on` | when the device last powered on |
| `last_off` | the start of the last outage |
| `last_seen` | the device's last answer |
| `checked` | the last probe. If it's more than a few intervals old, the watch isn't running |

For example, an alert script can tell whether a network drop was the router rebooting on power
return (`last_on` within a minute or two of the drop).

## Things to know

- **Privacy.** A public channel tells anyone when your home has no power. Messages don't include
  an address or IPs. Use `POWER_WATCH_LABEL` only if you want a place name in them, and use a
  private channel if you'd rather choose who sees it.
- **If the internet also goes down during an outage,** posts wait and go out once Telegram is
  reachable, with the real times in them. They also survive a restart of the service.
- An outage shorter than about a minute, or one that happens while the box itself is offline, is
  reported as "back at HH:MM after a short or unobserved outage". So is anything else that
  reboots the device: unplugging it, a firmware update.
- If Telegram accepts a post but the reply is lost on the way back, the retry posts it twice.
  Telegram has no way to deduplicate it, and it's rare.
- A setting with a bad value stops the service (exit 2) with the reason in the journal, instead of
  restarting forever.
- Only miIO devices for now. A generic ping/TCP probe would work with any device, but it gives
  no power-on time, so it couldn't tell a Wi-Fi hiccup from a real outage. PRs welcome.

## Українською

power-watch повідомляє в Telegram-канал, коли вдома зникає і повертається світло. Сам «датчик» —
будь-який Wi-Fi пристрій Xiaomi (лампа, розетка), увімкнений у звичайну розетку (не в ДБЖ). Роутер
і Raspberry Pi мають бути на ДБЖ. Для українських повідомлень у `/etc/power-watch.env` вкажіть
`POWER_WATCH_LANG=uk`.

```
⚡ Світла немає з 14:02.
✅ Світло повернулося о 16:40. Не було 2 год 38 хв.
```

## License

MIT
