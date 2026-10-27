# Contributing

Thanks for helping! Bug reports, new device models, translations and fixes are all welcome.

## Getting started

```sh
git clone https://github.com/levadadenys/power-watch && cd power-watch
python3 -m unittest discover -s tests -t .              # standard library only
pip install cryptography                                # optional: runs the purifier tests too
pip install -r requirements-dev.txt                     # ruff and mypy, pinned
ruff check . && ruff format --check . && mypy
python3 -m power_watch --scan 192.168.0.0/24            # run from the checkout
```

No Raspberry Pi needed: the tests fake the network, the clock and Telegram.

## How the code is laid out

`power_watch/__init__.py` maps the modules. The rule worth knowing: `state.py` is pure (no I/O,
no clock), so outage logic is changed and tested there, as a sequence of probes, not in the main
loop. `tests/helpers.py` has a `Probes` helper that drives it on a 15-second clock.

## Making a change

- **Keep the public channel honest.** A false "power is out" or "power is back" post is the worst
  failure. Any change to detection needs a test that walks the sequence that could go wrong.
- **Standard library only** for the core. `cryptography` stays optional (purifier readings only),
  and the power watch must keep running when it is missing or broken.
- **Never log a URL or a token.** The Telegram bot token is part of every request URL.
- One topic per pull request; CI (tests on 3.9–3.13, ruff, mypy, shellcheck) must pass.
- Update `README.md` and `CHANGELOG.md` when behaviour or settings change.

## Common contributions

**A language.** Add a block to `MESSAGES` in `power_watch/messages.py` and in
`power_watch/purifier.py`. `POWER_WATCH_LANG` accepts it from then on, and the tests check that
both tables have every language with every key.

**An air purifier model.** Add its property map to `MODELS` in `power_watch/purifier.py`, from the
model's spec on [home.miot-spec.com](https://home.miot-spec.com) (`siid`/`piid` pairs), and check
that its mode and fault values match. Please include the output of `power-watch --purifier` with
your device in the pull request.

## Reporting a bug

Open an issue with the journal lines around it (`journalctl -u power-watch --since "1 hour ago"`)
and what you expected. Remove tokens and anything that identifies your home before posting.
