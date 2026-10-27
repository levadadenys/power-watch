import builtins
import hashlib
import os
import tempfile
import unittest
from unittest import mock

from power_watch import store, watch
from power_watch.config import Config
from power_watch.state import new_state
from tests.helpers import KYIV, T0

PURIFIER = Config(purifier_ip="192.168.0.155", purifier_token="ab" * 16)


def failing_import(exc):
    """An __import__ that raises exc for `from power_watch import purifier`."""
    real = builtins.__import__

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "power_watch.purifier" or (name == "power_watch" and "purifier" in (fromlist or ())):
            raise exc
        return real(name, globals, locals, fromlist, level)

    return mock.patch("builtins.__import__", fake)


class PurifierLoading(unittest.TestCase):
    def load(self, exc):
        with failing_import(exc), self.assertLogs("power_watch.watch", "WARNING") as logs:
            self.assertIsNone(watch.load_purifier(PURIFIER))
        return logs.output[0]

    def test_missing_cryptography_says_what_to_install(self):
        self.assertIn("install python3-cryptography", self.load(ModuleNotFoundError(name="cryptography.hazmat")))

    def test_broken_cryptography_or_module_never_stops_the_watch(self):
        self.assertIn("OPENSSL_x", self.load(ImportError("undefined symbol: OPENSSL_x")))
        self.assertIn("SyntaxError", self.load(SyntaxError("half-written file")))

    def test_half_configured_purifier_is_logged(self):
        with self.assertLogs("power_watch.watch", "WARNING") as logs:
            self.assertIsNone(watch.load_purifier(Config(purifier_ip="192.168.0.155")))
        self.assertIn("needs both", logs.output[0])


class Loop(unittest.TestCase):
    """The real Watch.probe_once with the network replaced: posts, persisted and live state."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        self.cfg = Config(
            device="192.0.2.10",
            token="T",
            chat="@c",
            lang="en",
            tz=KYIV,
            gateway="192.0.2.1",
            run_dir=self.dir,
            state_dir=self.dir,
        )

    def run_probes(self, w, answers, start=T0, device_id=7, find=None):
        """answers: device uptime per probe (None = silent); the router always answers. find: what a
        network search for the sensor returns (None: not found anywhere)."""
        now = start
        with (
            mock.patch("power_watch.probe.gateway_up", return_value=True),
            mock.patch("power_watch.probe.find", return_value=find) as search,
            self.assertLogs("power_watch"),
        ):
            for up in answers:
                reply = None if up is None else (device_id, up)
                with mock.patch("power_watch.probe.hello", return_value=reply):
                    w.probe_once(now)
                now += self.cfg.interval
        self.searches = search.call_count
        return now

    def test_an_outage_is_posted_persisted_and_survives_a_restart(self):
        w = watch.Watch(self.cfg)
        boot = T0 - 3_600
        now = self.run_probes(w, [T0 - boot + 15 * i for i in range(10)] + [None] * 10)
        self.assertEqual(w.st["power"], "off")
        self.assertEqual(len(w.tg.pending()), 1)
        self.assertTrue(w.tg.pending()[0].startswith("⚡ Power is out since"))

        # Restart mid-outage: nothing re-posted, the unsent post is still there; then power returns.
        w2 = watch.Watch(self.cfg)
        self.assertEqual((w2.st["power"], len(w2.tg.pending())), ("off", 1))
        self.run_probes(w2, [None] * 5 + [30], start=now)
        posts = w2.tg.pending()
        self.assertEqual(len(posts), 2)
        self.assertTrue(posts[1].startswith("✅ Power is back at"))

        with open(os.path.join(self.dir, "state")) as f:
            self.assertIn("power=on", f.read())
        restored = new_state(0)
        self.assertEqual(len(store.load_state(os.path.join(self.dir, "state.json"), restored)), 2)
        self.assertEqual(restored["power"], "on")

    def test_run_reports_unsent_posts_and_shifts_times_on_a_clock_step(self):
        store.save_state(os.path.join(self.dir, "state.json"), {**new_state(T0), "power": "off"}, ["⚡ queued"])
        w = watch.Watch(self.cfg)
        started = w.st["started"]

        class Stop(Exception):
            pass

        # NTP steps the wall clock an hour forward during the first sleep; the monotonic one doesn't
        # move. The second sleep ends the loop. (logging reads time.time() too, so it is a function.)
        clock = {"wall": float(T0), "sleeps": 0}

        def sleep(_s):
            clock["sleeps"] += 1
            if clock["sleeps"] == 2:
                raise Stop
            clock["wall"] += 3_600

        with (
            mock.patch("time.time", side_effect=lambda: clock["wall"]),
            mock.patch("time.monotonic", return_value=100.0),
            mock.patch("time.sleep", side_effect=sleep),
            mock.patch.object(watch.Telegram, "start"),
            mock.patch.object(watch.Commands, "start"),
            mock.patch("power_watch.probe.hello", return_value=None),
            mock.patch("power_watch.probe.gateway_up", return_value=True),
            self.assertLogs("power_watch.watch") as logs,
            self.assertRaises(Stop),
        ):
            w.run()
        self.assertIn("1 unsent post(s)", logs.output[0])
        self.assertIn("wall clock stepped +3600 s", logs.output[1])
        self.assertEqual(w.st["started"], started + 3_600)

    def drive(self, w, hello, find, probes, start=T0 + 150, monotonic=100.0):
        """probe_once n times with fake network functions; returns (searches, warnings logged)."""
        clock = {"m": monotonic}
        with (
            mock.patch("power_watch.probe.hello", side_effect=hello),
            mock.patch("power_watch.probe.gateway_up", return_value=True),
            mock.patch("power_watch.probe.find", side_effect=find) as search,
            mock.patch("time.monotonic", side_effect=lambda: clock["m"]),
            self.assertLogs("power_watch") as logs,
        ):
            for i in range(probes):
                self.now = start + 15 * i
                w.probe_once(self.now)
                clock["m"] += 15
        return search.call_count, [r.getMessage() for r in logs.records if r.levelname in ("WARNING", "ERROR")]

    def lamp(self):
        """The sensor's hello reply at the current fake time: id 7, on since an hour before T0."""
        return (7, self.now - (T0 - 3_600))

    def test_a_sensor_moved_by_dhcp_is_followed_not_reported_as_an_outage(self):
        w = watch.Watch(self.cfg)
        self.run_probes(w, [3_600])  # learns the sensor's id at the configured IP
        self.assertEqual(w.st["device_id"], 7)
        moved = "192.0.2.99"
        _, warnings = self.drive(w, lambda ip: self.lamp() if ip == moved else None, lambda *a: moved, 10)
        self.assertEqual((w.device_ip, w.st["power"], w.tg.pending()), (moved, "on", []))
        self.assertIn("moved 192.0.2.10 → 192.0.2.99", warnings[0])

    def test_a_move_inside_the_search_window_is_still_found_before_declaring(self):
        # The sensor drops off Wi-Fi: the first silent probe searches and finds nothing (it isn't back
        # yet), arming the 5-minute limit. It rejoins at a new address 20 s later. The search forced
        # right before the declaring miss must find it: no "out" post, no false alarm.
        w = watch.Watch(self.cfg)
        self.run_probes(w, [3_600] * 10)
        moved, rejoined = "192.0.2.99", {"yes": False}

        def find(*_a):
            return moved if rejoined["yes"] else None

        def hello(ip):
            rejoined["yes"] = rejoined["yes"] or w.st["misses"] >= 1
            return self.lamp() if ip == moved and rejoined["yes"] else None

        searches, _ = self.drive(w, hello, find, 12)
        self.assertEqual((w.st["power"], w.tg.pending(), w.device_ip), ("on", [], moved))
        self.assertEqual(searches, 2)

    def test_a_replaced_sensor_never_causes_an_outage_and_says_how_to_fix_it(self):
        w = watch.Watch(self.cfg)
        self.run_probes(w, [3_600] * 10)
        # A new lamp at the same address answers as device 99; device 7 is nowhere.
        _, errors = self.drive(w, lambda ip: (99, 60), lambda *a: None, 40)
        self.assertEqual((w.st["power"], w.tg.pending()), ("on", []))
        self.assertEqual(len([e for e in errors if "POWER_WATCH_DEVICE_ID=99" in e]), 1)  # once per 30 min
        # The user sets the new id: the new lamp is the sensor from now on, with a silent baseline
        # (not a "power came back" post for its new power-on time).
        w2 = watch.Watch(Config(**{**self.cfg.__dict__, "device_id": 99}))
        self.assertEqual((w2.st["device_id"], w2.st["power"]), (99, "unknown"))
        self.drive(w2, lambda ip: (99, 120), lambda *a: None, 1)
        self.assertEqual((w2.st["last_on"], w2.tg.pending()), (T0 + 150 - 120, []))

    def test_setting_the_id_before_it_was_ever_learned_keeps_an_ongoing_outage(self):
        path = os.path.join(self.dir, "state.json")
        store.save_state(path, {**new_state(T0), "power": "off", "last_off": T0, "device_cfg": "192.0.2.10"}, [])
        w = watch.Watch(Config(**{**self.cfg.__dict__, "device_id": 7}))
        self.assertEqual((w.st["device_id"], w.st["power"]), (7, "off"))
        self.drive(w, lambda ip: (7, 30), lambda *a: None, 1)  # power returns
        self.assertTrue(w.tg.pending()[0].startswith("✅ Power is back"))

    def reconfigured(self, device="192.0.2.10", device_id=0):
        """A Watch restarted with new settings, after a run that knew sensor 7 at 192.0.2.10 and was
        mid-outage."""
        path = os.path.join(self.dir, "state.json")
        st = {**new_state(T0), "power": "off", "last_off": T0, "last_on": T0 - 3_600}
        store.save_state(path, {**st, "device_id": 7, "device_cfg": "192.0.2.10"}, [])
        return watch.Watch(Config(**{**self.cfg.__dict__, "device": device, "device_id": device_id}))

    def test_a_new_sensor_at_a_new_ip_starts_from_a_silent_baseline(self):
        for device_id in (0, 99):  # learned on its first answer, or set explicitly
            with self.subTest(device_id=device_id):
                w = self.reconfigured(device="192.0.2.20", device_id=device_id)
                self.drive(w, lambda ip: (99, 60), lambda *a: None, 1)
                self.assertEqual((w.st["device_id"], w.st["power"], w.tg.pending()), (99, "on", []))

    def test_the_same_lamp_under_a_new_ip_keeps_its_outage(self):
        w = self.reconfigured(device="192.0.2.20")
        self.drive(w, lambda ip: (7, 30), lambda *a: None, 1)  # same lamp, power just came back
        self.assertTrue(w.tg.pending()[0].startswith("✅ Power is back"))

    def test_an_unknown_device_breaks_the_silence_streak(self):
        w = watch.Watch(self.cfg)
        self.run_probes(w, [3_600] * 10)
        # Three silent probes (one short of "out"), then an unknown device answers at the address for a
        # while, then nothing: a single silent probe must not finish the old count.
        replies = iter([None] * 3 + [(99, 60)] * 4 + [None])
        self.drive(w, lambda ip: next(replies), lambda *a: None, 8)
        self.assertEqual((w.st["power"], w.st["misses"], w.tg.pending()), ("on", 1, []))

    def test_an_unknown_device_never_forces_a_search_on_every_probe(self):
        cfg = Config(**{**self.cfg.__dict__, "off_after": 1})  # misses == off_after - 1 == 0 always
        w = watch.Watch(cfg)
        self.run_probes(w, [3_600] * 5)
        searches, _ = self.drive(w, lambda ip: (99, 60), lambda *a: None, 40)  # 10 minutes
        self.assertLessEqual(searches, 3)

    def test_searches_are_rate_limited_and_more_frequent_during_an_outage(self):
        w = watch.Watch(self.cfg)
        self.run_probes(w, [3_600] * 10)
        # 30 minutes of real outage (nothing found): a search on the first silent probe, one forced
        # before declaring, then one a minute while "off".
        searches, _ = self.drive(w, lambda ip: None, lambda *a: None, 120)
        self.assertEqual(w.st["power"], "off")
        self.assertTrue(28 <= searches <= 32, searches)

    def test_a_newly_configured_device_forgets_the_old_id(self):
        path = os.path.join(self.dir, "state.json")
        store.save_state(path, {**new_state(T0), "device_id": 7, "device_cfg": "192.0.2.50"}, [])
        self.assertEqual(watch.Watch(self.cfg).st["device_id"], 0)
        store.save_state(path, {**new_state(T0), "device_id": 7, "device_cfg": "192.0.2.10"}, [])
        self.assertEqual(watch.Watch(self.cfg).st["device_id"], 7)

    @staticmethod
    def purifier_key(token):
        return f"192.0.2.155 {hashlib.sha256(token.encode()).hexdigest()[:12]}"

    def test_a_new_purifier_token_forgets_the_old_id(self):
        store.save_state(
            os.path.join(self.dir, "state.json"),
            {**new_state(T0), "purifier_id": 42, "purifier_cfg": self.purifier_key("cd" * 16)},
            [],
        )
        w, dev = self.purifier_watch()
        self.assertEqual((w.st["purifier_id"], dev.did), (0, 0))

    def purifier_watch(self, saved_id=0):
        if saved_id:
            store.save_state(
                os.path.join(self.dir, "state.json"),
                {**new_state(T0), "purifier_id": saved_id, "purifier_cfg": self.purifier_key("ab" * 16)},
                [],
            )
        cfg = Config(**{**self.cfg.__dict__, "purifier_ip": "192.0.2.155", "purifier_token": "ab" * 16})
        dev = mock.Mock(did=0, ip="192.0.2.155")
        module = mock.Mock()
        module.Device.return_value = dev
        with mock.patch("power_watch.watch.load_purifier", return_value=watch.LoadedPurifier(module, dev)):
            return watch.Watch(cfg), dev

    def test_a_purifier_that_moved_while_the_service_was_down_is_found(self):
        w, dev = self.purifier_watch(saved_id=42)
        self.assertEqual(dev.did, 42)  # known before it ever answers
        with (
            mock.patch("power_watch.probe.hello", return_value=None),
            mock.patch("power_watch.probe.find", return_value="192.0.2.154"),
            self.assertLogs("power_watch.watch"),
        ):
            w.follow_purifier()
            w.follow_purifier()  # rate-limited: no second check
        self.assertEqual(dev.ip, "192.0.2.154")

    def test_a_purifier_in_place_is_not_searched_for_and_its_id_is_saved(self):
        w, dev = self.purifier_watch()
        dev.did = 42  # learned from a /status handshake
        with (
            mock.patch("power_watch.probe.hello", return_value=(42, 100)),
            mock.patch("power_watch.probe.find") as find,
        ):
            w.follow_purifier()
        self.assertEqual((find.call_count, w.st["purifier_id"]), (0, 42))

    def test_status_adds_purifier_lines_and_never_fails_because_of_them(self):
        w = watch.Watch(Config(device="192.0.2.10"))
        broken = mock.Mock()
        broken.status.return_value = ["🌫 The purifier isn't answering."]
        w.purifier = watch.LoadedPurifier(broken, object())
        self.assertEqual(w.status().splitlines()[1], "🌫 The purifier isn't answering.")


if __name__ == "__main__":
    unittest.main()
