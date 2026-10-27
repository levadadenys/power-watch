"""Purifier tests against a fake device on localhost (skipped without python3-cryptography)."""

import json
import logging
import socket
import struct
import threading
import time
import unittest

try:
    from power_watch import purifier as pu
except ImportError:  # the optional dependency is missing: nothing here can run
    pu = None  # type: ignore[assignment]


def status_and_logs(dev):
    """purifier.status() plus what it logged (assertLogs fails when nothing is logged)."""
    records: list = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("power_watch.purifier")
    logger.addHandler(handler)
    propagate, logger.propagate = logger.propagate, False
    try:
        return pu.status(dev, "en"), [r.getMessage() for r in records]
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate


TOKEN = "00112233445566778899aabbccddeeff"
DID = 12345678  # made up


class FakePurifier:
    """A miIO device on localhost: answers hello, then decrypts requests with the device's own
    copy of the token and answers miIO.info / get_properties."""

    def __init__(
        self,
        token=TOKEN,
        model="xiaomi.airp.cpa4",
        values=None,
        rows=None,
        raw_result=None,
        errors=(),
        spoof=False,
        chatty=False,
        garbled=False,
    ):
        self.dev = pu.Device("127.0.0.1", token)
        self.model = model
        self.values = values or {"on": True, "fault": 0, "mode": 0, "pm25": 7, "filter_pct": 63, "filter_days": 120}
        self.rows, self.raw_result = rows, raw_result  # override the get_properties result
        self.errors = list(errors)  # error objects to answer the next requests with
        self.spoof, self.chatty, self.garbled = spoof, chatty, garbled
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # "another LAN host"
        self.requests, self.raw = [], []
        self.closed = False
        threading.Thread(target=self.serve, daemon=True).start()

    def close(self):
        self.closed = True
        self.sock.close()
        self.other.close()

    def serve(self):
        while not self.closed:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:  # closed
                return
            if data == pu.HELLO:
                self.sock.sendto(struct.pack(">HHIII", 0x2131, 32, 0, DID, 1_000) + b"\xff" * 16, addr)
                continue
            try:
                req = self.dev.open(data)
            except Exception:  # noqa: BLE001 — wrong token on the client side: a real device stays silent
                continue
            self.requests.append(req)
            self.raw.append(self.dev.decrypt(data[32:]))
            if self.spoof:  # junk from another host, ahead of the real reply
                self.other.sendto(b"\x21\x31" + b"\0" * 40, addr)
            if self.garbled:  # the device itself: a packet that fails the checksum, then the reply
                self.sock.sendto(b"\x21\x31" + b"\0" * 40, addr)
            if self.chatty:  # valid packets for other request ids, forever
                for i in range(200):
                    if self.closed:
                        return
                    wrong = json.dumps({"id": req["id"] + 1 + i, "result": []}).encode()
                    self.sock.sendto(self.dev.packet(DID, 1_001, wrong), addr)
                    time.sleep(0.01)
                continue
            if self.errors:
                body = {"id": req["id"], "error": self.errors.pop(0)}
            elif req["method"] == "miIO.info":
                body = {"id": req["id"], "result": {"model": self.model}}
            elif self.raw_result is not None:
                raw = self.raw_result
                body = {"id": req["id"], **raw} if isinstance(raw, dict) else raw
            else:
                body = {
                    "id": req["id"],
                    "result": self.rows
                    if self.rows is not None
                    else [
                        {
                            "did": p["did"],
                            "siid": p["siid"],
                            "piid": p["piid"],
                            "code": 0,
                            "value": self.values[p["did"]],
                        }
                        for p in req["params"]
                    ],
                }
            payload = json.dumps(body).encode() + b"\0"
            self.sock.sendto(self.dev.packet(DID, 1_001, payload), addr)

    def client(self, token=TOKEN):
        dev = pu.Device("127.0.0.1", token, timeout=0.5)
        dev.port = self.sock.getsockname()[1]
        return dev


class FakeCase(unittest.TestCase):
    def fake(self, **kwargs):
        fake = FakePurifier(**kwargs)
        self.addCleanup(fake.close)
        return fake


@unittest.skipIf(pu is None, "python3-cryptography not installed")
class Protocol(FakeCase):
    def test_reads_the_model_and_its_properties(self):
        fake = self.fake()
        dev = fake.client()
        self.assertEqual(dev.read(), fake.values)
        self.assertEqual(dev.model, "xiaomi.airp.cpa4")
        props = fake.requests[-1]["params"]
        self.assertIn({"did": "pm25", "siid": 3, "piid": 4}, props)

    def test_wrong_token_is_a_clean_error(self):
        fake = self.fake()
        with self.assertRaises(pu.MiioError):
            fake.client(token="ff" * 16).read()
        lines, logged = status_and_logs(fake.client(token="ff" * 16))
        self.assertEqual(lines, ["🌫 The purifier isn't answering."])
        self.assertIn("wrong", logged[0])

    def test_a_handshake_records_the_device_id_and_rejects_another_one(self):
        dev = self.fake().client()
        status_and_logs(dev)
        self.assertEqual(dev.did, 12345678)
        dev.did = 42  # the purifier is device 42; another device now answers at its address
        lines, logged = status_and_logs(dev)
        self.assertEqual((lines, dev.did), (["🌫 The purifier isn't answering."], 42))
        self.assertIn("a different device (id 12345678)", logged[0])

    def test_unknown_model_says_so(self):
        dev = self.fake(model="zhimi.airp.new1").client()
        self.assertEqual(status_and_logs(dev)[0], ["🌫 Purifier model zhimi.airp.new1 isn't supported yet."])

    def test_requests_end_with_a_nul_like_python_miio(self):
        fake = self.fake()
        fake.client().read()
        self.assertTrue(all(raw.endswith(b"\0") for raw in fake.raw))

    def test_junk_from_another_host_is_dropped(self):
        fake = self.fake(spoof=True)
        self.assertEqual(fake.client().read(), fake.values)

    def test_a_garbled_packet_from_the_device_is_skipped(self):
        fake = self.fake(garbled=True)
        self.assertEqual(fake.client().read(), fake.values)

    def test_transient_device_error_is_retried(self):
        fake = self.fake(errors=[{"code": -9999, "message": "user ack timeout"}])
        self.assertEqual(fake.client().read(), fake.values)

    def test_other_device_errors_are_not_retried(self):
        fake = self.fake(errors=[{"code": -5001, "message": "invalid params"}])
        with self.assertRaises(pu.MiioError):
            fake.client().read()
        self.assertEqual(len(fake.requests), 1)

    def test_a_read_never_exceeds_its_budget(self):
        dev = self.fake(chatty=True).client()
        # One request alone may take 1.5 s; the whole read (info, properties, retries) gets 0.5 s.
        dev.timeout, dev.budget, dev.model = 1.5, 0.5, ""
        t0 = time.monotonic()
        with self.assertRaises(pu.MiioError):
            dev.read()
        self.assertLess(time.monotonic() - t0, 1.1)  # without the budget: at least 1.5 s

    def test_a_stream_of_unrelated_replies_cannot_hold_the_call_open(self):
        dev = self.fake(chatty=True).client()
        t0 = time.monotonic()
        with self.assertRaises(pu.MiioError):
            dev.read()
        self.assertLess(time.monotonic() - t0, 2.0)  # attempts × timeout (1 s), with CI slack

    def test_odd_replies_become_lines_never_exceptions(self):
        cases = {
            "non-dict reply (ignored until the deadline)": dict(raw_result=["not", "a", "dict"]),
            "non-dict error": dict(errors=["boom"]),
            "non-list result": dict(raw_result={"result": 5}),
            "pm25 null": dict(
                values={"on": True, "fault": 0, "mode": "x", "pm25": None, "filter_pct": 1, "filter_days": 2}
            ),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                lines, _ = status_and_logs(self.fake(**kwargs).client())
                self.assertTrue(lines and all(isinstance(line, str) for line in lines), lines)

    def test_failed_properties_are_reported_not_hidden(self):
        props = pu.MODELS["xiaomi.airp.cpa4"]
        failed = [{"did": n, "siid": s, "piid": p, "code": -4001} for n, (s, p) in props.items()]
        lines, logged = status_and_logs(self.fake(rows=failed).client())
        self.assertEqual(lines, ["🌫 The purifier answered, but without readings."])
        self.assertIn("-4001", logged[0])
        partial = [
            {"did": "pm25", "siid": 3, "piid": 4, "code": 0},  # no value
            {"did": "filter_pct", "siid": 4, "piid": 1, "code": 0, "value": 40},
        ]
        self.assertEqual(self.fake(rows=partial).client().read(), {"filter_pct": 40})

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
        self.assertEqual(
            pu.status_lines(values, "en"),
            ["🌫 PM2.5: 7 µg/m³ (good)", "Purifier: auto", "🧻 Filter: 63% (~120 days left)"],
        )
        self.assertEqual(
            pu.status_lines(values, "uk"), ["🌫 PM2.5: 7 мкг/м³ (добре)", "Очищувач: авто", "🧻 Фільтр: 63% (≈ 120 дн.)"]
        )

    def test_off_fault_and_climate(self):
        lines = pu.status_lines({"on": False, "mode": 1, "fault": 2, "humidity": 45, "temperature": 22.4}, "en")
        self.assertEqual(lines, ["💧 45%   🌡 22.4 °C", "Purifier: off", "⚠️ Purifier: motor stuck"])

    def test_quality_bands(self):
        self.assertEqual(
            [pu.pm25_quality(v, "en") for v in (9, 10, 35, 36, 55, 56)],
            [
                "good",
                "moderate",
                "moderate",
                "unhealthy for sensitive groups",
                "unhealthy for sensitive groups",
                "unhealthy",
            ],
        )

    def test_every_language_has_every_key(self):
        self.assertEqual(set(pu.MESSAGES["en"]), set(pu.MESSAGES["uk"]))


if __name__ == "__main__":
    unittest.main()
