import http.client
import io
import socket
import threading
import unittest
import urllib.error
from unittest import mock

from power_watch.telegram import Telegram, chats_from_updates, describe


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body):
        super().__init__("https://example.invalid", code, "x", {}, io.BytesIO())  # type: ignore[arg-type]
        self.body = body

    def read(self, *_):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Send(unittest.TestCase):
    def send_with(self, effect):
        if isinstance(effect, urllib.error.HTTPError):
            self.addCleanup(effect.close)
        with mock.patch("urllib.request.urlopen", side_effect=effect), self.assertLogs("power_watch", "WARNING"):
            return Telegram("T", "@c").send("hi")

    def test_errors_are_retried_or_dropped_never_raised(self):
        self.assertEqual(self.send_with(http.client.BadStatusLine("x")), ("retry", 30))
        self.assertEqual(self.send_with(TimeoutError()), ("retry", 30))
        self.assertEqual(self.send_with(FakeHTTPError(502, http.client.IncompleteRead(b""))), ("retry", 30))
        self.assertEqual(self.send_with(FakeHTTPError(400, b'{"description": "chat not found"}')), ("dropped", 0))

    def test_429_honours_retry_after(self):
        for body, wait in ((b'{"parameters": {"retry_after": 7}}', 7.0), (b"[]", 30)):
            err = FakeHTTPError(429, body)
            self.addCleanup(err.close)
            with mock.patch("urllib.request.urlopen", side_effect=err):
                self.assertEqual(Telegram("T", "@c").send("hi"), ("retry", wait))

    def test_the_token_never_reaches_the_log(self):
        with (
            mock.patch("urllib.request.urlopen", side_effect=OSError("https://api.telegram.org/botSECRET/x")),
            self.assertLogs("power_watch") as logs,
        ):
            Telegram("SECRET", "@c").send("hi")
        self.assertNotIn("SECRET", "".join(logs.output))
        self.assertIn("OSError: https://api.telegram.org/bot<token>/x", "".join(logs.output))


TOKEN = "123456:SECRET"


class Describe(unittest.TestCase):
    def test_says_why_the_network_failed(self):
        dns = urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution"))
        self.assertEqual(describe(dns, TOKEN), "URLError: gaierror: [Errno -3] Temporary failure in name resolution")
        unreachable = urllib.error.URLError(OSError(101, "Network is unreachable"))
        self.assertEqual(describe(unreachable, TOKEN), "URLError: OSError: [Errno 101] Network is unreachable")
        self.assertEqual(describe(urllib.error.URLError("no host given"), TOKEN), "URLError: no host given")
        self.assertEqual(describe(TimeoutError("timed out"), TOKEN), "TimeoutError: timed out")
        self.assertEqual(describe(TimeoutError(), TOKEN), "TimeoutError")

    def test_scrubs_the_token_and_stays_short(self):
        err = urllib.error.URLError("unknown url type: xhttps://api.telegram.org/botSECRET/getUpdates")
        self.assertEqual(
            describe(err, "SECRET"), "URLError: unknown url type: xhttps://api.telegram.org/bot<token>/getUpdates"
        )
        self.assertLessEqual(len(describe(ValueError("x" * 1000), TOKEN)), 200)


class Outbox(unittest.TestCase):
    def test_worker_survives_a_crash_and_sends_in_order(self):
        sent, done = [], threading.Event()
        tg = Telegram("T", "@c", outbox=["first"], on_sent=lambda: len(sent) == 2 and done.set())
        calls = iter([RuntimeError("boom")])

        def send(text):
            err = next(calls, None)
            if err:
                raise err
            sent.append(text)
            return "sent", 0

        tg.send = send  # type: ignore[method-assign,assignment]
        with mock.patch("time.sleep"), self.assertLogs("power_watch.telegram", "WARNING"):
            tg.start()
            tg.post("second")
            self.assertTrue(done.wait(5))
        self.assertEqual((sent, tg.pending()), (["first", "second"], []))


class Chats(unittest.TestCase):
    def test_chats_from_updates(self):
        updates = {
            "ok": True,
            "result": [
                {"update_id": 1, "my_chat_member": {"chat": {"id": -1001234, "type": "channel", "title": "Світло"}}},
                {"update_id": 2, "channel_post": {"chat": {"id": -1001234, "type": "channel", "title": "Світло"}}},
                {"update_id": 3, "message": {"chat": {"id": 42, "type": "private", "username": "someone"}}},
                {"update_id": 4, "edited_message": {}},
            ],
        }
        self.assertEqual(chats_from_updates(updates), {-1001234: "channel Світло", 42: "private someone"})


if __name__ == "__main__":
    unittest.main()
