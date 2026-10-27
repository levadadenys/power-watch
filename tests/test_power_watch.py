"""Tests for power_watch: python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import power_watch as pw  # noqa: E402

INTERVAL = 15
OFF_AFTER = 4
T0 = 1_793_000_000
DEVICE_BOOT = T0 - 3_600  # device on for an hour when the watch starts
KYIV = ZoneInfo("Europe/Kyiv")


class Watch:
    """Drives step() one probe at a time on a 15 s clock."""

    def __init__(self, **saved):
        self.now = T0
        self.st = {'power': 'unknown', 'since': T0, 'last_on': 0, 'last_off': 0, 'last_seen': 0, 'checked': 0,
                   'misses': 0, 'gw_streak': 0, 'prev_since': T0, 'prev_last_off': 0, 'started': T0, **saved}
        self.events = []

    def probe(self, boot=None, gw_ok=True, n=1):
        for _ in range(n):
            up = None if boot is None else self.now - boot
            self.events += pw.step(self.st, up, gw_ok, self.now, INTERVAL, OFF_AFTER)
            self.now += INTERVAL
        return self

    def device(self, boot=DEVICE_BOOT, n=1):
        return self.probe(boot, True, n)

    def silent(self, n=1):
        return self.probe(None, True, n)

    def router_down(self, n=1):
        return self.probe(None, False, n)

    def kinds(self):
        return [k for k, _ in self.events]


class StateMachine(unittest.TestCase):
    def test_first_answer_is_a_silent_baseline(self):
        w = Watch().device()
        self.assertEqual((w.st['power'], w.st['last_on']), ('on', DEVICE_BOOT))
        self.assertEqual(w.kinds(), ['baseline'])

    def test_four_misses_with_router_up_is_an_outage_then_reboot_is_the_return(self):
        w = Watch().device(n=10).silent(n=3)
        self.assertEqual(w.st['power'], 'on', 'three misses are not an outage')
        w.silent()
        self.assertEqual(w.st['power'], 'off')
        self.assertEqual(w.events[-1], ('off', {'off_at': w.st['last_off'], 'at_start': False}))
        off_at = w.st['last_off']
        w.silent(n=20)
        back = w.now - 5
        w.device(boot=back)
        self.assertEqual((w.st['power'], w.st['last_on']), ('on', back))
        self.assertEqual(w.events[-1], ('on', {'boot': back, 'off_at': off_at}))

    def test_misses_split_by_a_router_reboot_do_not_add_up(self):
        w = Watch().device(n=10).silent(n=3).router_down(n=8).silent()
        self.assertEqual(w.st['power'], 'on')

    def test_device_rejoining_wifi_after_a_router_reboot_is_not_an_outage(self):
        # Router back, device silent for 4 probes (rejoining Wi-Fi): grace, not "off".
        w = Watch().device(n=10).router_down(n=8).silent(n=4)
        self.assertEqual(w.st['power'], 'on')
        # Silent much longer with the router up: a real outage after all.
        w.silent(n=4)
        self.assertEqual(w.st['power'], 'off')

    def test_power_return_seen_only_as_a_reboot_between_answers(self):
        w = Watch().device(n=10).router_down(n=8)
        back = w.now - 60
        w.device(boot=back)
        self.assertEqual((w.st['power'], w.st['last_on']), ('on', back))
        self.assertEqual(w.events[-1], ('unobserved', {'boot': back}))

    def test_off_that_ends_without_a_reboot_was_a_wifi_gap(self):
        w = Watch().device(n=10)
        since, last_off = w.st['since'], w.st['last_off']
        w.silent(n=10)
        self.assertEqual(w.st['power'], 'off')
        w.device()  # same boot time as before: it never lost power
        self.assertEqual((w.st['power'], w.st['since'], w.st['last_off'], w.st['last_on']),
                         ('on', since, last_off, DEVICE_BOOT))
        self.assertEqual(w.kinds()[-1], 'false_alarm')

    def test_reboot_before_off_was_declared_is_still_a_power_return(self):
        # Power returns and the device reboots during the router grace, but rejoins Wi-Fi only
        # after "off" was declared: boot < last_off, yet it is a real power return.
        w = Watch().device(n=10).router_down(n=8)
        back = w.now + 10
        w.silent(n=8)
        self.assertEqual(w.st['power'], 'off')
        self.assertLess(back, w.st['last_off'] - pw.BOOT_JUMP_S, 'the case a last_off comparison gets wrong')
        w.device(boot=back)
        self.assertEqual((w.st['power'], w.st['last_on']), ('on', back))
        self.assertEqual(w.kinds()[-1], 'on')

    def test_watch_started_during_an_outage(self):
        w = Watch().silent(n=10)
        self.assertEqual(w.st['power'], 'off')
        self.assertTrue(w.events[-1][1]['at_start'])
        back = w.now - 20
        w.device(boot=back).device(boot=back)
        self.assertEqual((w.st['power'], w.st['last_on']), ('on', back))
        self.assertEqual(w.events[1:], [('on', {'boot': back, 'off_at': None})])

    def test_device_slow_to_join_at_start_is_a_false_alarm(self):
        # "Off" declared before the first answer, but the device has been up since before the
        # watch started: it was only slow to show up.
        w = Watch().silent(n=10).device()
        self.assertEqual(w.kinds(), ['off', 'false_alarm'])
        self.assertEqual(w.st['power'], 'on')

    def test_last_seen_tracks_the_last_answer(self):
        w = Watch().device(n=3)
        seen = w.now - INTERVAL
        w.router_down(n=2)
        self.assertEqual(w.st['last_seen'], seen)

    def test_router_unreachable_never_declares_off(self):
        w = Watch().device().router_down(n=100)
        self.assertEqual(w.st['power'], 'on')

    def test_restart_during_an_outage_does_not_repeat_it(self):
        # Restored from state.json: already off. More silence posts nothing new; the return does.
        off_at = T0 - 600
        w = Watch(power='off', since=off_at, last_on=DEVICE_BOOT, last_off=off_at).silent(n=20)
        self.assertEqual(w.events, [])
        back = w.now - 5
        w.device(boot=back)
        self.assertEqual(w.events, [('on', {'boot': back, 'off_at': off_at})])


class Messages(unittest.TestCase):
    now = datetime(2026, 10, 27, 18, 0, tzinfo=KYIV).timestamp()

    def at(self, h, m, day=27):
        return datetime(2026, 10, day, h, m, tzinfo=KYIV).timestamp()

    def test_outage_and_return_in_both_languages(self):
        off = ('off', {'off_at': self.at(14, 2), 'at_start': False})
        on = ('on', {'boot': self.at(16, 40), 'off_at': self.at(14, 2)})
        self.assertEqual(pw.message(*off, 'en', KYIV, self.now), '⚡ Power is out since 14:02.')
        self.assertEqual(pw.message(*on, 'en', KYIV, self.now), '✅ Power is back at 16:40, after 2 h 38 min.')
        self.assertEqual(pw.message(*off, 'uk', KYIV, self.now), '⚡ Світла немає з 14:02.')
        self.assertEqual(pw.message(*on, 'uk', KYIV, self.now), '✅ Світло повернулося о 16:40. Не було 2 год 38 хв.')

    def test_short_outage_is_in_minutes(self):
        on = ('on', {'boot': self.at(14, 10), 'off_at': self.at(14, 2)})
        self.assertEqual(pw.message(*on, 'en', KYIV, self.now), '✅ Power is back at 14:10, after 8 min.')

    def test_return_without_a_known_start_has_no_duration(self):
        for off_at in (None, self.at(14, 12)):  # unknown start; device booted before "off" was declared
            on = ('on', {'boot': self.at(14, 10), 'off_at': off_at})
            self.assertEqual(pw.message(*on, 'en', KYIV, self.now), '✅ Power is back at 14:10.')

    def test_other_days_show_the_date(self):
        off = ('off', {'off_at': self.at(23, 50, day=26), 'at_start': False})
        self.assertEqual(pw.message(*off, 'en', KYIV, self.now), '⚡ Power is out since 26.10 23:50.')

    def test_label_prefix_and_silent_events(self):
        ev = ('false_alarm', {'boot': 0})
        self.assertTrue(pw.message(*ev, 'en', KYIV, self.now, 'Home').startswith('Home: ↩️'))
        self.assertIsNone(pw.message('baseline', {'boot': 0}, 'en', KYIV, self.now))

    def test_every_event_kind_has_a_journal_line_and_every_language_every_key(self):
        for kind, ev in [('off', {'off_at': T0, 'at_start': False}), ('on', {'boot': T0, 'off_at': None}),
                         ('unobserved', {'boot': T0}), ('false_alarm', {'boot': T0}), ('baseline', {'boot': T0})]:
            self.assertTrue(pw.describe(kind, ev))
        self.assertEqual(set(pw.MESSAGES['en']), set(pw.MESSAGES['uk']))


class Plumbing(unittest.TestCase):
    def test_hello_reply_parsing(self):
        reply = bytes.fromhex('21310020' + '00000000' + '0a0b0c0d' + '00000e10') + b'\0' * 16
        self.assertEqual(pw.parse_hello(reply), (0x0a0b0c0d, 3600))
        self.assertIsNone(pw.parse_hello(b'\x00' * 32))
        self.assertIsNone(pw.parse_hello(reply[:10]))

    def test_persisted_state_round_trip_ignores_unknown_keys(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'state.json')
            pw.write_atomic(path, json.dumps({'power': 'off', 'last_off': 5, 'misses': 99}))
            st = {'power': 'unknown', 'last_off': 0, 'misses': 0}
            pw.load_state(path, st)
            self.assertEqual(st, {'power': 'off', 'last_off': 5, 'misses': 0})
            pw.load_state(os.path.join(d, 'missing.json'), st)  # no file: unchanged, no error
            self.assertEqual(st['power'], 'off')


if __name__ == '__main__':
    unittest.main()
