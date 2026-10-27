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
import power_watch as pw

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
                   'misses': 0, 'gw_streak': 0, 'gw_down': 0, 'prev_since': T0, 'prev_last_off': 0, 'started': T0,
                   **saved}
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


    def test_device_clock_drift_never_adds_up_to_a_reboot(self):
        # A slow device clock moves the computed boot time later every answer (here 1 s per
        # probe: days of real drift); compared with the first sighting it would cross BOOT_JUMP_S.
        w = Watch()
        for k in range(200):
            w.device(boot=DEVICE_BOOT + k)
        self.assertEqual(w.kinds(), ['baseline'])

    def test_one_failed_router_probe_does_not_restart_the_count(self):
        w = Watch().device(n=10).silent(n=2).router_down().silent(n=2)
        self.assertEqual(w.kinds()[-1], 'off')

    def test_restart_after_an_outage_seen_only_from_the_start(self):
        # Run 1 started during an outage (last_on never known); power came back while the watch
        # was down, before run 2 started: a real return, not a false alarm.
        off_at = T0 - 3_600
        w = Watch(power='off', since=off_at, last_on=0, last_off=off_at)
        back = T0 - 600
        w.device(boot=back)
        self.assertEqual(w.events, [('on', {'boot': back, 'off_at': None})])

    def test_clock_step_shifts_stored_times(self):
        st = {'since': 100, 'last_on': 50, 'last_off': 0, 'last_seen': 120, 'checked': 120,
              'prev_since': 10, 'prev_last_off': 0, 'started': 100}
        pw.shift_times(st, 3_600)
        self.assertEqual((st['since'], st['last_on'], st['last_off'], st['started']), (3_700, 3_650, 0, 3_700))


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

    def test_persisted_state_round_trip_ignores_unknown_and_mistyped_keys(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'state.json')
            pw.write_atomic(path, json.dumps({'power': 'off', 'last_off': 5, 'last_on': '7', 'misses': 99,
                                              'outbox': ['a', 3, 'b']}))
            st = {'power': 'unknown', 'last_off': 0, 'last_on': 0, 'misses': 0}
            self.assertEqual(pw.load_state(path, st), ['a', 'b'])
            self.assertEqual(st, {'power': 'off', 'last_off': 5, 'last_on': 0, 'misses': 0})
            self.assertEqual(pw.load_state(os.path.join(d, 'missing.json'), st), [])  # no file: no error
            pw.write_atomic(path, '[1, 2]')
            self.assertEqual(pw.load_state(path, st), [])
            self.assertEqual(st['power'], 'off')


    def test_chats_from_updates(self):
        updates = {'ok': True, 'result': [
            {'update_id': 1, 'my_chat_member': {'chat': {'id': -1001234, 'type': 'channel', 'title': 'Світло'}}},
            {'update_id': 2, 'channel_post': {'chat': {'id': -1001234, 'type': 'channel', 'title': 'Світло'}}},
            {'update_id': 3, 'message': {'chat': {'id': 42, 'type': 'private', 'username': 'someone'}}},
            {'update_id': 4, 'edited_message': {}}]}
        self.assertEqual(pw.chats_from_updates(updates), {-1001234: 'channel Світло', 42: 'private someone'})


class Bot(unittest.TestCase):
    now = datetime(2026, 10, 27, 18, 0, tzinfo=KYIV).timestamp()

    def msg(self, text, user=7, chat_type='private', username='me'):
        return {'update_id': 1, 'message': {'chat': {'id': user, 'type': chat_type},
                                            'from': {'id': user, 'username': username}, 'text': text}}

    def test_status_text(self):
        since = datetime(2026, 10, 27, 11, 23, tzinfo=KYIV).timestamp()
        self.assertEqual(pw.status_text({'power': 'on', 'since': since}, 'uk', KYIV, self.now),
                         '💡 Світло є з 11:23 (6 год 37 хв).')
        self.assertEqual(pw.status_text({'power': 'off', 'since': since}, 'en', KYIV, self.now),
                         '⚡ Power is out since 11:23 (6 h 37 min).')
        self.assertTrue(pw.status_text({'power': 'unknown', 'since': since}, 'en', KYIV, self.now).startswith('❔'))

    def test_status_flags_a_silent_sensor_and_long_spans_in_days(self):
        since = self.now - 5 * 86_400 - 3 * 3_600  # across the 10-25 DST change: 16:00 local
        st = {'power': 'on', 'since': since, 'last_seen': self.now - 120}
        self.assertEqual(pw.status_text(st, 'en', KYIV, self.now),
                         "💡 Power is on since 22.10 16:00 (5 d 3 h). The sensor hasn't answered for 2 min.")
        st['last_seen'] = self.now - 10
        self.assertNotIn('sensor', pw.status_text(st, 'en', KYIV, self.now))

    def test_a_backlog_gets_one_fresh_reply_per_chat_and_strangers_are_logged_once(self):
        sent, logged = [], []
        c = pw.Commands(None, frozenset({7}), lambda: 'STATUS', 'en')
        c.strangers = set()
        c.answer = lambda chat, text: sent.append((chat, text))
        orig, pw.log = pw.log, logged.append
        try:
            fresh = [dict(self.msg('/status'), update_id=i) for i in range(5)]
            for u in fresh:
                u['message']['date'] = self.now - 30
            old = self.msg('/status', user=8)
            old['message']['date'] = self.now - 3_600
            c.allowed = frozenset({7, 8})
            strangers = [self.msg('/status', user=99) for _ in range(3)]
            for u in strangers:
                u['message']['date'] = self.now
            c.handle([*fresh, old, *strangers], self.now)
        finally:
            pw.log = orig
        self.assertEqual(sent, [(7, 'STATUS')])
        self.assertEqual(len(logged), 1)

    def test_allowed_users_get_the_status_others_are_logged(self):
        allowed, status = frozenset({7}), lambda: 'STATUS'
        for text in ('/status', '/status@pi_power_watch_chiki_bot', '/start', '?', 'Статус', 'світло'):
            self.assertEqual(pw.reply_for(self.msg(text), allowed, status, 'en'), (7, 'STATUS'), text)
        self.assertEqual(pw.reply_for(self.msg('hello'), allowed, status, 'en'), (7, pw.MESSAGES['en']['help']))
        self.assertEqual(pw.reply_for(self.msg('', ), allowed, status, 'en'), (7, pw.MESSAGES['en']['help']))
        stranger = pw.reply_for(self.msg('/status', user=99, username='x'), allowed, status, 'en')
        self.assertIsInstance(stranger, str)
        self.assertIn('99', stranger)
        self.assertNotIn('STATUS', stranger)

    def test_groups_channels_and_other_updates_are_ignored(self):
        allowed, status = frozenset({7}), lambda: 'STATUS'
        self.assertIsNone(pw.reply_for(self.msg('/status', chat_type='group'), allowed, status, 'en'))
        self.assertIsNone(pw.reply_for({'update_id': 2, 'channel_post': {'chat': {'id': -100}}}, allowed, status, 'en'))
        self.assertIsNone(pw.reply_for({'update_id': 3, 'message': 'garbage'}, allowed, status, 'en'))


class Config(unittest.TestCase):
    def test_defaults(self):
        cfg = pw.config({'POWER_WATCH_DEVICE_IP': '192.168.0.150'})
        self.assertEqual(
            (cfg['device'], cfg['lang'], cfg['interval'], cfg['off_after'], cfg['gw_port'], cfg['allowed']),
            ('192.168.0.150', 'en', 15.0, 4, 53, frozenset()))
        self.assertEqual(pw.config({'POWER_WATCH_ALLOWED_USERS': '7, 42 8'})['allowed'], frozenset({7, 42, 8}))

    def test_bad_values_are_config_errors(self):
        for env in ({'POWER_WATCH_OFF_AFTER': '4   # misses'}, {'POWER_WATCH_OFF_AFTER': '0'},
                    {'POWER_WATCH_INTERVAL_S': '0'}, {'POWER_WATCH_TZ': 'Europe/Kyiv  # x'},
                    {'POWER_WATCH_TZ': '../etc'}, {'POWER_WATCH_LANG': 'de'},
                    {'POWER_WATCH_DEVICE_IP': '192.168.0.150 # lamp'}, {'POWER_WATCH_GATEWAY': 'router'},
                    {'POWER_WATCH_ALLOWED_USERS': '@me'}):
            with self.subTest(env=env), self.assertRaises(pw.ConfigError):
                pw.config(env)


class FakeHTTPError(pw.urllib.error.HTTPError):
    def __init__(self, code, body):
        super().__init__('https://example.invalid', code, 'x', {}, None)
        self.body = body

    def read(self, *_):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class TelegramSender(unittest.TestCase):
    def send_with(self, effect):
        def fake_urlopen(*_a, **_k):
            raise effect
        orig, pw.urllib.request.urlopen = pw.urllib.request.urlopen, fake_urlopen
        try:
            return pw.Telegram('T', '@c').send('hi')
        finally:
            pw.urllib.request.urlopen = orig

    def test_errors_are_retried_or_dropped_never_raised(self):
        import http.client
        self.assertEqual(self.send_with(http.client.BadStatusLine('x')), ('retry', 30))
        self.assertEqual(self.send_with(TimeoutError()), ('retry', 30))
        self.assertEqual(self.send_with(FakeHTTPError(502, http.client.IncompleteRead(b''))), ('retry', 30))
        self.assertEqual(self.send_with(FakeHTTPError(429, b'{"parameters": {"retry_after": 7}}')), ('retry', 7.0))
        self.assertEqual(self.send_with(FakeHTTPError(429, b'[]')), ('retry', 30))
        self.assertEqual(self.send_with(FakeHTTPError(400, b'{"description": "chat not found"}')), ('dropped', 0))

    def test_worker_survives_a_crash_and_sends_in_order(self):
        import threading
        sent, done = [], threading.Event()
        tg = pw.Telegram('T', '@c', outbox=['first'], on_sent=lambda: len(sent) == 2 and done.set())
        calls = iter([RuntimeError('boom')])

        def send(text):
            err = next(calls, None)
            if err:
                raise err
            sent.append(text)
            return 'sent', 0
        tg.send = send
        orig_sleep, pw.time.sleep = pw.time.sleep, lambda _s: None
        try:
            tg.start()
            tg.post('second')
            self.assertTrue(done.wait(5))
        finally:
            pw.time.sleep = orig_sleep
        self.assertEqual((sent, tg.pending()), (['first', 'second'], []))


if __name__ == '__main__':
    unittest.main()
