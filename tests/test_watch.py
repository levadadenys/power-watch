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
        self.cfg = Config(device="192.0.2.10", token="T", chat="@c", lang="en", tz=KYIV, gateway="192.0.2.1")

    def run_probes(self, w, answers, start=T0):
        """answers: device uptime per probe (None = silent); the router always answers."""
        now = start
        with mock.patch("power_watch.probe.gateway_up", return_value=True), self.assertLogs("power_watch"):
            for up in answers:
                with mock.patch("power_watch.probe.device_uptime", return_value=up):
                    w.probe_once(now)
                now += self.cfg.interval
        return now

    def test_an_outage_is_posted_persisted_and_survives_a_restart(self):
        w = watch.Watch(self.cfg, self.dir, self.dir)
        boot = T0 - 3_600
        now = self.run_probes(w, [T0 - boot + 15 * i for i in range(10)] + [None] * 10)
        self.assertEqual(w.st["power"], "off")
        self.assertEqual(len(w.tg.pending()), 1)
        self.assertTrue(w.tg.pending()[0].startswith("⚡ Power is out since"))

        # Restart mid-outage: nothing re-posted, the unsent post is still there; then power returns.
        w2 = watch.Watch(self.cfg, self.dir, self.dir)
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

    def test_status_adds_purifier_lines_and_never_fails_because_of_them(self):
        w = watch.Watch(self.cfg)
        broken = mock.Mock()
        broken.status.return_value = ["🌫 The purifier isn't answering."]
        w.purifier = (broken, object())
        self.assertEqual(w.status().splitlines()[1], "🌫 The purifier isn't answering.")


if __name__ == "__main__":
    unittest.main()
