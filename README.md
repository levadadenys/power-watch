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

One Python file, no dependencies, no cloud, no device token.

## How it works

- **Probe.** Xiaomi Wi-Fi devices speak the miIO protocol. A 32-byte "hello" to UDP port 54321
  gets a reply without any pairing or token. The reply carries the device's **uptime**, so
  `now − uptime` is the exact moment it last powered on.
- **Outage.** The device stops answering (4 probes in a row, about a minute) while the router still
  answers. If the router itself is unreachable, the device's silence means nothing and is ignored.
- **Return.** The device answers again with a new power-on time. That time is the moment power came
  back, even if the network was down at that moment. Many routers reboot when mains returns and
  the UPS switches back, so the "power is back" post usually goes out a couple of minutes late
  with the correct time.
- **No false alarms from Wi-Fi.** If the device comes back *without* having rebooted, power never
  went out, and the channel gets a correction. After the router reboots, the device gets a minute
  of grace to rejoin Wi-Fi before its silence counts.
- **Restarts.** The power state survives restarts (`/var/lib/power-watch/state.json`, written only
  on changes), so a restart neither repeats nor loses an outage.

## You need

- An always-on Linux box with systemd and Python 3.10+, on the same LAN as the device. Put it and the
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

**3. Telegram**

1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` and copy the token.
2. Create a channel. Make it public with an @name, or private and share an invite link.
3. Channel settings → Administrators → add your bot, with permission to post messages.
4. The chat id is `@channelname` for a public channel. For a private one, use its numeric `-100…` id.
   Forward a channel post to [@userinfobot](https://t.me/userinfobot), or briefly make the channel
   public to test.

**4. Install**

```sh
sudo install -m 755 power_watch.py /usr/local/bin/power-watch
sudo install -m 644 systemd/power-watch.service /etc/systemd/system/
sudo install -m 600 power-watch.env.example /etc/power-watch.env
sudoedit /etc/power-watch.env          # device IP, token, chat id, language
sudo systemctl daemon-reload && sudo systemctl enable --now power-watch.service
```

**5. Check**

```sh
sudo bash -c 'set -a; . /etc/power-watch.env; power-watch --test-notify'   # a test post in the channel
journalctl -u power-watch -f                                                # one line per change
cat /run/power-watch/state                                                  # live state
```

Then unplug the device: after about a minute the channel says the power is out, and when you
plug it back in it says the power is back.

## Configuration

`/etc/power-watch.env` (see [`power-watch.env.example`](power-watch.env.example)):

| Variable | Default | |
|---|---|---|
| `POWER_WATCH_DEVICE_IP` | — | the miIO device on mains (required) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | without them, only the journal |
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
  reachable, with the real times in them. Posts still waiting when the service restarts are lost,
  but the state isn't.
- An outage shorter than about a minute, or one that happens while the box itself is offline, is
  reported as "back at HH:MM after a short or unobserved outage".
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
