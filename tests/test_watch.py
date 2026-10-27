import builtins
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

    def test_a_sensor_moved_by_dhcp_is_followed_not_reported_as_an_outage(self):
        w = watch.Watch(self.cfg)
        self.run_probes(w, [3_600])  # learns the sensor's id at the configured IP
        self.assertEqual(w.st["device_id"], 7)
        moved = "192.0.2.99"

        def hello(ip):  # the old address is silent; the sensor answers at its new one
            return (7, 3_700) if ip == moved else None

        with (
            mock.patch("power_watch.probe.hello", side_effect=hello),
            mock.patch("power_watch.probe.gateway_up", return_value=True),
            mock.patch("power_watch.probe.find", return_value=moved),
            self.assertLogs("power_watch.watch", "WARNING") as logs,
        ):
            for i in range(10):
                w.probe_once(T0 + 15 * (i + 1))
        self.assertEqual((w.device_ip, w.st["power"], w.tg.pending()), (moved, "on", []))
        self.assertIn("moved 192.0.2.10 → 192.0.2.99", logs.output[0])

    def test_another_device_at_the_sensors_address_is_silence(self):
        w = watch.Watch(self.cfg)
        self.run_probes(w, [3_600] * 10)
        # DHCP gave the address to a different miIO device; the sensor itself is off.
        self.run_probes(w, [60] * 6, device_id=99, start=T0 + 150)
        self.assertEqual(w.st["power"], "off")
        self.assertTrue(w.tg.pending()[0].startswith("⚡"))

    def test_searches_are_rate_limited_during_a_real_outage(self):
        w = watch.Watch(self.cfg)
        self.run_probes(w, [3_600] * 10)
        self.run_probes(w, [None] * 40, start=T0 + 150)  # 10 minutes of silence, nothing found
        self.assertEqual(self.searches, 1)  # monotonic time barely moves in a test: one search only

    def test_a_newly_configured_device_forgets_the_old_id(self):
        store.save_state(
            os.path.join(self.dir, "state.json"), {**new_state(T0), "device_id": 7, "device_cfg": "192.0.2.50"}, []
        )
        self.assertEqual(watch.Watch(self.cfg).st["device_id"], 0)
        store.save_state(
            os.path.join(self.dir, "state.json"), {**new_state(T0), "device_id": 7, "device_cfg": "192.0.2.10"}, []
        )
        self.assertEqual(watch.Watch(self.cfg).st["device_id"], 7)

    def test_a_purifier_that_stopped_answering_is_looked_for(self):
        w = watch.Watch(self.cfg)
        dev = mock.Mock(failed=True, did=42, ip="192.0.2.155")
        w.purifier = watch.LoadedPurifier(mock.Mock(), dev)
        with mock.patch("power_watch.probe.find", return_value="192.0.2.154"), self.assertLogs("power_watch.watch"):
            w.follow_purifier()
            w.follow_purifier()  # rate-limited: no second search
        self.assertEqual(dev.ip, "192.0.2.154")

    def test_status_adds_purifier_lines_and_never_fails_because_of_them(self):
        w = watch.Watch(Config(device="192.0.2.10"))
        broken = mock.Mock()
        broken.status.return_value = ["🌫 The purifier isn't answering."]
        w.purifier = watch.LoadedPurifier(broken, object())
        self.assertEqual(w.status().splitlines()[1], "🌫 The purifier isn't answering.")


if __name__ == "__main__":
    unittest.main()
