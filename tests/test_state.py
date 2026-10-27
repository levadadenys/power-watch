import unittest

from power_watch.state import BOOT_JUMP_S, describe, shift_times
from tests.helpers import DEVICE_BOOT, INTERVAL, T0, Probes


class Outages(unittest.TestCase):
    def test_first_answer_is_a_silent_baseline(self):
        w = Probes().device()
        self.assertEqual((w.st["power"], w.st["last_on"]), ("on", DEVICE_BOOT))
        self.assertEqual(w.kinds(), ["baseline"])

    def test_four_misses_with_router_up_is_an_outage_then_reboot_is_the_return(self):
        w = Probes().device(n=10).silent(n=3)
        self.assertEqual(w.st["power"], "on", "three misses are not an outage")
        w.silent()
        self.assertEqual(w.st["power"], "off")
        self.assertEqual(w.events[-1], ("off", {"off_at": w.st["last_off"], "at_start": False}))
        off_at = w.st["last_off"]
        w.silent(n=20)
        back = w.now - 5
        w.device(boot=back)
        self.assertEqual((w.st["power"], w.st["last_on"]), ("on", back))
        self.assertEqual(w.events[-1], ("on", {"boot": back, "off_at": off_at}))

    def test_watch_started_during_an_outage(self):
        w = Probes().silent(n=10)
        self.assertEqual(w.st["power"], "off")
        self.assertTrue(w.events[-1][1]["at_start"])
        back = w.now - 20
        w.device(boot=back).device(boot=back)
        self.assertEqual((w.st["power"], w.st["last_on"]), ("on", back))
        self.assertEqual(w.events[1:], [("on", {"boot": back, "off_at": None})])

    def test_last_seen_tracks_the_last_answer(self):
        w = Probes().device(n=3)
        seen = w.now - INTERVAL
        w.router_down(n=2)
        self.assertEqual(w.st["last_seen"], seen)


class RouterReboots(unittest.TestCase):
    def test_misses_split_by_a_router_reboot_do_not_add_up(self):
        w = Probes().device(n=10).silent(n=3).router_down(n=8).silent()
        self.assertEqual(w.st["power"], "on")

    def test_device_rejoining_wifi_after_a_router_reboot_is_not_an_outage(self):
        # Router back, device silent for 4 probes (rejoining Wi-Fi): grace, not "off".
        w = Probes().device(n=10).router_down(n=8).silent(n=4)
        self.assertEqual(w.st["power"], "on")
        # Silent much longer with the router up: a real outage after all.
        w.silent(n=4)
        self.assertEqual(w.st["power"], "off")

    def test_power_return_seen_only_as_a_reboot_between_answers(self):
        w = Probes().device(n=10).router_down(n=8)
        back = w.now - 60
        w.device(boot=back)
        self.assertEqual((w.st["power"], w.st["last_on"]), ("on", back))
        self.assertEqual(w.events[-1], ("unobserved", {"boot": back}))

    def test_reboot_before_off_was_declared_is_still_a_power_return(self):
        # Power returns and the device reboots during the router grace, but rejoins Wi-Fi only
        # after "off" was declared: boot < last_off, yet it is a real power return.
        w = Probes().device(n=10).router_down(n=8)
        back = w.now + 10
        w.silent(n=8)
        self.assertEqual(w.st["power"], "off")
        self.assertLess(back, w.st["last_off"] - BOOT_JUMP_S, "the case a last_off comparison gets wrong")
        w.device(boot=back)
        self.assertEqual((w.st["power"], w.st["last_on"]), ("on", back))
        self.assertEqual(w.kinds()[-1], "on")

    def test_router_unreachable_never_declares_off(self):
        w = Probes().device().router_down(n=100)
        self.assertEqual(w.st["power"], "on")

    def test_one_failed_router_probe_does_not_restart_the_count(self):
        w = Probes().device(n=10).silent(n=2).router_down().silent(n=2)
        self.assertEqual(w.kinds()[-1], "off")


class FalseAlarms(unittest.TestCase):
    def test_off_that_ends_without_a_reboot_was_a_wifi_gap(self):
        w = Probes().device(n=10)
        since, last_off = w.st["since"], w.st["last_off"]
        w.silent(n=10)
        self.assertEqual(w.st["power"], "off")
        w.device()  # same boot time as before: it never lost power
        self.assertEqual(
            (w.st["power"], w.st["since"], w.st["last_off"], w.st["last_on"]), ("on", since, last_off, DEVICE_BOOT)
        )
        self.assertEqual(w.kinds()[-1], "false_alarm")

    def test_device_slow_to_join_at_start_is_a_false_alarm(self):
        # "Off" declared before the first answer, but the device has been up since before the
        # watch started: it was only slow to show up.
        w = Probes().silent(n=10).device()
        self.assertEqual(w.kinds(), ["off", "false_alarm"])
        self.assertEqual(w.st["power"], "on")

    def test_device_clock_drift_never_adds_up_to_a_reboot(self):
        # A slow device clock moves the computed boot time later every answer (here 1 s per
        # probe: days of real drift); compared with the first sighting it would cross BOOT_JUMP_S.
        w = Probes()
        for k in range(200):
            w.device(boot=DEVICE_BOOT + k)
        self.assertEqual(w.kinds(), ["baseline"])


class Restarts(unittest.TestCase):
    def test_restart_during_an_outage_does_not_repeat_it(self):
        # Restored from state.json: already off. More silence posts nothing new; the return does.
        off_at = T0 - 600
        w = Probes(power="off", since=off_at, last_on=DEVICE_BOOT, last_off=off_at).silent(n=20)
        self.assertEqual(w.events, [])
        back = w.now - 5
        w.device(boot=back)
        self.assertEqual(w.events, [("on", {"boot": back, "off_at": off_at})])

    def test_restart_after_an_outage_seen_only_from_the_start(self):
        # Run 1 started during an outage (last_on never known); power came back while the watch
        # was down, before run 2 started: a real return, not a false alarm.
        off_at = T0 - 3_600
        w = Probes(power="off", since=off_at, last_on=0, last_off=off_at)
        back = T0 - 600
        w.device(boot=back)
        self.assertEqual(w.events, [("on", {"boot": back, "off_at": None})])

    def test_clock_step_shifts_stored_times(self):
        st = {
            "since": 100,
            "last_on": 50,
            "last_off": 0,
            "last_seen": 120,
            "checked": 120,
            "prev_since": 10,
            "prev_last_off": 0,
            "started": 100,
        }
        shift_times(st, 3_600)
        self.assertEqual((st["since"], st["last_on"], st["last_off"], st["started"]), (3_700, 3_650, 0, 3_700))


class JournalLines(unittest.TestCase):
    def test_every_event_kind_has_one(self):
        for kind, ev in [
            ("off", {"off_at": T0, "at_start": False}),
            ("on", {"boot": T0, "off_at": None}),
            ("unobserved", {"boot": T0}),
            ("false_alarm", {"boot": T0}),
            ("baseline", {"boot": T0}),
        ]:
            self.assertTrue(describe(kind, ev))


if __name__ == "__main__":
    unittest.main()
