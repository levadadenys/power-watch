import unittest

from power_watch.bot import Commands, reply_for
from power_watch.messages import MESSAGES
from tests.helpers import kyiv

NOW = kyiv(18, 0)


def msg(text, user=7, chat_type="private", username="me", date=NOW):
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": user, "type": chat_type},
            "date": date,
            "from": {"id": user, "username": username},
            "text": text,
        },
    }


def status():
    return "STATUS"


class Replies(unittest.TestCase):
    def test_allowed_users_get_the_status(self):
        for text in ("/status", "/status@my_power_watch_bot", "/start", "?", "Статус", "світло"):
            self.assertEqual(reply_for(msg(text), frozenset({7}), status, "en"), (7, "STATUS"), text)
        self.assertEqual(reply_for(msg("hello"), frozenset({7}), status, "en"), (7, MESSAGES["en"]["help"]))
        self.assertEqual(reply_for(msg(""), frozenset({7}), status, "en"), (7, MESSAGES["en"]["help"]))

    def test_strangers_get_nothing_but_a_log_line_with_their_id(self):
        stranger = reply_for(msg("/status", user=99, username="x"), frozenset({7}), status, "en")
        self.assertIsInstance(stranger, str)
        self.assertIn("99", stranger)
        self.assertNotIn("STATUS", stranger)

    def test_groups_channels_and_other_updates_are_ignored(self):
        allowed = frozenset({7})
        self.assertIsNone(reply_for(msg("/status", chat_type="group"), allowed, status, "en"))
        self.assertIsNone(reply_for({"update_id": 2, "channel_post": {"chat": {"id": -100}}}, allowed, status, "en"))
        self.assertIsNone(reply_for({"update_id": 3, "message": "garbage"}, allowed, status, "en"))


class Batches(unittest.TestCase):
    def commands(self, allowed, status_fn):
        sent = []
        c = Commands(None, frozenset(allowed), status_fn, "en")  # type: ignore[arg-type]
        c.answer = lambda chat, text: sent.append((chat, text))  # type: ignore[method-assign]
        return c, sent

    def test_a_backlog_gets_one_fresh_reply_per_chat_and_strangers_are_logged_once(self):
        c, sent = self.commands({7, 8}, status)
        fresh = [dict(msg("/status", date=NOW - 30), update_id=i) for i in range(5)]
        old = msg("/status", user=8, date=NOW - 3_600)
        strangers = [msg("/status", user=99) for _ in range(3)]
        with self.assertLogs("power_watch.bot") as logs:
            c.handle([*fresh, old, *strangers], NOW)
        self.assertEqual(sent, [(7, "STATUS")])
        self.assertEqual(len(logs.records), 1)

    def test_a_batch_builds_the_status_once(self):
        calls = []
        c, sent = self.commands({7, 8}, lambda: calls.append(1) or "STATUS")
        c.handle([msg("/status", user=u) for u in (7, 8, 7, 8)], NOW)
        self.assertEqual((len(calls), sorted(sent)), (1, [(7, "STATUS"), (8, "STATUS")]))


if __name__ == "__main__":
    unittest.main()
