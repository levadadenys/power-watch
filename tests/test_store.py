import json
import os
import tempfile
import unittest
from unittest import mock

from power_watch import probe, store
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

    def test_device_identity_round_trips_with_its_types_checked(self):
        store.save_state(self.path, {**new_state(1), "device_id": 7, "device_cfg": "192.0.2.10"}, [])
        st = new_state(2)
        store.load_state(self.path, st)
        self.assertEqual((st["device_id"], st["device_cfg"]), (7, "192.0.2.10"))
        store.write_atomic(self.path, json.dumps({"device_id": "7", "device_cfg": 5}))
        st = new_state(2)
        store.load_state(self.path, st)
        self.assertEqual((st["device_id"], st["device_cfg"]), (0, ""))

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


class Find(unittest.TestCase):
    def test_find_matches_the_device_id_not_the_address(self):
        found = {"192.0.2.5": (1, 10), "192.0.2.9": (7, 20)}
        with (
            mock.patch("power_watch.probe.scan", return_value=found) as scan,
            mock.patch("power_watch.probe.lan_of", return_value="192.0.2.0/24"),
        ):
            self.assertEqual(probe.find(7, "192.0.2.150"), "192.0.2.9")
            self.assertIsNone(probe.find(8, "192.0.2.150"))
        self.assertEqual(scan.call_args[0][0], "192.0.2.0/24")


class Lan(unittest.TestCase):
    ROUTES = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"  # default via 192.168.1.1
        "eth0\t0000A8C0\t00000000\t0001\t0\t0\t100\t00FEFFFF\t0\t0\t0\n"  # 192.168.0.0/23
    )

    def test_the_search_covers_the_whole_lan_not_just_the_24(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=self.ROUTES)):
            self.assertEqual(probe.lan_of("192.168.1.240"), "192.168.0.0/23")
            self.assertEqual(probe.lan_of("10.0.0.5"), "10.0.0.5/24")  # not a local network: its /24


class Hello(unittest.TestCase):
    def test_reply_parsing(self):
        reply = bytes.fromhex("21310020" + "00000000" + "0a0b0c0d" + "00000e10") + b"\0" * 16
        self.assertEqual(parse_hello(reply), (0x0A0B0C0D, 3600))
        self.assertIsNone(parse_hello(b"\x00" * 32))
        self.assertIsNone(parse_hello(reply[:10]))


if __name__ == "__main__":
    unittest.main()
