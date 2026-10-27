import json
import os
import tempfile
import unittest

from power_watch import store
from power_watch.probe import parse_hello
from power_watch.state import new_state


class StateFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "state.json")

    def test_round_trip_with_the_outbox(self):
        st = {**new_state(100), "power": "off", "last_off": 90}
        store.save_state(self.path, st, ["⚡ out"])
        restored = new_state(200)
        self.assertEqual(store.load_state(self.path, restored), ["⚡ out"])
        self.assertEqual((restored["power"], restored["last_off"], restored["started"]), ("off", 90, 200))

    def test_unknown_and_mistyped_keys_are_ignored(self):
        store.write_atomic(
            self.path,
            json.dumps({"power": "off", "last_off": 5, "last_on": "7", "misses": 99, "outbox": ["a", 3, "b"]}),
        )
        st = {"power": "unknown", "last_off": 0, "last_on": 0, "misses": 0}
        self.assertEqual(store.load_state(self.path, st), ["a", "b"])
        self.assertEqual(st, {"power": "off", "last_off": 5, "last_on": 0, "misses": 0})

    def test_missing_or_odd_files_are_no_state(self):
        st = new_state(1)
        self.assertEqual(store.load_state(os.path.join(self.dir.name, "missing.json"), st), [])
        store.write_atomic(self.path, "[1, 2]")
        self.assertEqual(store.load_state(self.path, st), [])
        self.assertEqual(st["power"], "unknown")

    def test_live_file_has_exactly_the_public_keys(self):
        path = os.path.join(self.dir.name, "state")
        store.write_live(path, new_state(100))
        with open(path) as f:
            keys = [line.split("=")[0] for line in f.read().splitlines()]
        self.assertEqual(tuple(keys), store.PUBLIC_KEYS)


class Hello(unittest.TestCase):
    def test_reply_parsing(self):
        reply = bytes.fromhex("21310020" + "00000000" + "0a0b0c0d" + "00000e10") + b"\0" * 16
        self.assertEqual(parse_hello(reply), (0x0A0B0C0D, 3600))
        self.assertIsNone(parse_hello(b"\x00" * 32))
        self.assertIsNone(parse_hello(reply[:10]))


if __name__ == "__main__":
    unittest.main()
