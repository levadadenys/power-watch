"""Tests for purifier: python3 -m unittest discover -s tests (skipped without python3-cryptography)"""
import json
import socket
import struct
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import purifier as pu
except ImportError:  # the optional dependency is missing: nothing here can run
    pu = None

TOKEN = "00112233445566778899aabbccddeeff"
DID = 917817873


class FakePurifier:
    """A miIO device on localhost: answers hello, then decrypts requests with the device's own
    copy of the token and answers miIO.info / get_properties."""

    def __init__(self, token=TOKEN, model="xiaomi.airp.cpa4", values=None):
        self.dev = pu.Device("127.0.0.1", token)
        self.model = model
        self.values = values or {"on": True, "fault": 0, "mode": 0, "pm25": 7, "filter_pct": 63, "filter_days": 120}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.requests = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            data, addr = self.sock.recvfrom(4096)
            if data == pu.HELLO:
                self.sock.sendto(struct.pack(">HHIII", 0x2131, 32, 0, DID, 1_000) + b"\xff" * 16, addr)
                continue
            try:
                req = self.dev.open(data)
            except Exception:  # wrong token on the client side: a real device stays silent
                continue
            self.requests.append(req)
            if req["method"] == "miIO.info":
                result = {"model": self.model}
            else:
                result = [{"did": p["did"], "siid": p["siid"], "piid": p["piid"], "code": 0,
                           "value": self.values[p["did"]]} for p in req["params"]]
            payload = json.dumps({"id": req["id"], "result": result}).encode() + b"\0"
            self.sock.sendto(self.dev.packet(DID, 1_001, payload), addr)

    def client(self, token=TOKEN):
        dev = pu.Device("127.0.0.1", token, timeout=0.5)
        port = self.sock.getsockname()[1]
        dev_call = dev.call

        def call(method, params):  # point the client at the fake's port
            orig, pu.PORT = pu.PORT, port
            try:
                return dev_call(method, params)
            finally:
                pu.PORT = orig
        dev.call = call
        return dev


@unittest.skipIf(pu is None, "python3-cryptography not installed")
class Protocol(unittest.TestCase):
    def test_reads_the_model_and_its_properties(self):
        fake = FakePurifier()
        dev = fake.client()
        self.assertEqual(dev.read(), fake.values)
        self.assertEqual(dev.model, "xiaomi.airp.cpa4")
        props = fake.requests[-1]["params"]
        self.assertIn({"did": "pm25", "siid": 3, "piid": 4}, props)

    def test_wrong_token_is_a_clean_error(self):
        fake = FakePurifier()
        with self.assertRaises(pu.MiioError):
            fake.client(token="ff" * 16).read()
        self.assertEqual(pu.status(fake.client(token="ff" * 16), "en"), ["🌫 The purifier isn't answering."])

    def test_unknown_model_says_so(self):
        dev = FakePurifier(model="zhimi.airp.new1").client()
        self.assertEqual(pu.status(dev, "en"), ["🌫 Purifier model zhimi.airp.new1 isn't supported yet."])

    def test_packet_round_trip_and_tamper_check(self):
        dev = pu.Device("127.0.0.1", TOKEN)
        pkt = dev.packet(DID, 5, b'{"id": 1}')
        self.assertEqual(dev.open(pkt), {"id": 1})
        with self.assertRaises(pu.MiioError):
            dev.open(pkt[:40] + bytes([pkt[40] ^ 1]) + pkt[41:])


@unittest.skipIf(pu is None, "python3-cryptography not installed")
class Lines(unittest.TestCase):
    def test_status_lines_in_both_languages(self):
        values = {"on": True, "fault": 0, "mode": 0, "pm25": 7, "filter_pct": 63, "filter_days": 120}
        self.assertEqual(pu.status_lines(values, "en"),
                         ["🌫 PM2.5: 7 µg/m³ (good)", "Purifier: auto", "🧻 Filter: 63% (~120 days left)"])
        self.assertEqual(pu.status_lines(values, "uk"),
                         ["🌫 PM2.5: 7 мкг/м³ (добре)", "Очищувач: авто", "🧻 Фільтр: 63% (≈ 120 дн.)"])

    def test_off_fault_and_climate(self):
        lines = pu.status_lines({"on": False, "mode": 1, "fault": 2, "humidity": 45, "temperature": 22.4}, "en")
        self.assertEqual(lines, ["💧 45%   🌡 22.4 °C", "Purifier: off", "⚠️ Purifier: motor stuck"])

    def test_quality_bands(self):
        self.assertEqual([pu.pm25_quality(v, "en") for v in (9, 10, 35, 36, 55, 56)],
                         ["good", "moderate", "moderate", "unhealthy for sensitive groups",
                          "unhealthy for sensitive groups", "unhealthy"])

    def test_every_language_has_every_key(self):
        self.assertEqual(set(pu.MESSAGES["en"]), set(pu.MESSAGES["uk"]))


if __name__ == "__main__":
    unittest.main()
