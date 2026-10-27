import unittest

from power_watch.messages import MESSAGES, message, status_text
from tests.helpers import KYIV, kyiv

NOW = kyiv(18, 0)


class ChannelPosts(unittest.TestCase):
    def test_outage_and_return_in_both_languages(self):
        off = ("off", {"off_at": kyiv(14, 2), "at_start": False})
        on = ("on", {"boot": kyiv(16, 40), "off_at": kyiv(14, 2)})
        self.assertEqual(message(*off, "en", KYIV, NOW), "⚡ Power is out since 14:02.")
        self.assertEqual(message(*on, "en", KYIV, NOW), "✅ Power is back at 16:40, after 2 h 38 min.")
        self.assertEqual(message(*off, "uk", KYIV, NOW), "⚡ Світла немає з 14:02.")
        self.assertEqual(message(*on, "uk", KYIV, NOW), "✅ Світло повернулося о 16:40. Не було 2 год 38 хв.")

    def test_short_outage_is_in_minutes(self):
        on = ("on", {"boot": kyiv(14, 10), "off_at": kyiv(14, 2)})
        self.assertEqual(message(*on, "en", KYIV, NOW), "✅ Power is back at 14:10, after 8 min.")

    def test_return_without_a_known_start_has_no_duration(self):
        for off_at in (None, kyiv(14, 12)):  # unknown start; device booted before "off" was declared
            on = ("on", {"boot": kyiv(14, 10), "off_at": off_at})
            self.assertEqual(message(*on, "en", KYIV, NOW), "✅ Power is back at 14:10.")

    def test_other_days_show_the_date(self):
        off = ("off", {"off_at": kyiv(23, 50, day=26), "at_start": False})
        self.assertEqual(message(*off, "en", KYIV, NOW), "⚡ Power is out since 26.10 23:50.")

    def test_label_prefix_and_silent_events(self):
        self.assertTrue(message("false_alarm", {"boot": 0}, "en", KYIV, NOW, "Home").startswith("Home: ↩️"))
        self.assertIsNone(message("baseline", {"boot": 0}, "en", KYIV, NOW))

    def test_every_language_has_every_key(self):
        self.assertEqual(set(MESSAGES["en"]), set(MESSAGES["uk"]))


class StatusLine(unittest.TestCase):
    def test_on_off_unknown(self):
        since = kyiv(11, 23)
        self.assertEqual(
            status_text({"power": "on", "since": since}, "uk", KYIV, NOW), "💡 Світло є з 11:23 (6 год 37 хв)."
        )
        self.assertEqual(
            status_text({"power": "off", "since": since}, "en", KYIV, NOW), "⚡ Power is out since 11:23 (6 h 37 min)."
        )
        self.assertTrue(status_text({"power": "unknown", "since": since}, "en", KYIV, NOW).startswith("❔"))

    def test_flags_a_silent_sensor_and_long_spans_in_days(self):
        since = NOW - 5 * 86_400 - 3 * 3_600  # across the 10-25 DST change: 16:00 local
        st = {"power": "on", "since": since, "last_seen": NOW - 120}
        self.assertEqual(
            status_text(st, "en", KYIV, NOW),
            "💡 Power is on since 22.10 16:00 (5 d 3 h). The sensor hasn't answered for 2 min.",
        )
        st["last_seen"] = NOW - 10
        self.assertNotIn("sensor", status_text(st, "en", KYIV, NOW))


if __name__ == "__main__":
    unittest.main()
