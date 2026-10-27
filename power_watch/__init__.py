"""power-watch: know when the power goes out at home, and tell a Telegram channel.

A small always-on box (a Raspberry Pi) rides a UPS together with the router, so it never sees an
outage itself. A mains-powered Xiaomi Wi-Fi device does: it answers the miIO "hello" packet while
there is power and goes silent when there is none, and each reply carries the device's uptime, so
the moment power came back is exact even when the box was offline at the time.

Modules, in the order data flows:
    probe     the miIO hello, the router check, LAN scan (I/O)
    state     the outage state machine (pure)
    messages  channel and /status texts in each language (pure)
    store     the persisted and the live state files
    telegram  channel posts with a persisted outbox, Bot API errors
    bot       private /status replies for allowed users
    purifier  optional air purifier readings (encrypted miIO)
    config    validated settings from the environment
    watch     the main loop that ties them together
    cli       the power-watch command
"""

__version__ = "0.1.0"
