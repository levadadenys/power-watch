import contextlib
import io
import unittest
from unittest import mock

from power_watch import __version__, cli


class Cli(unittest.TestCase):
    def run_cli(self, *argv, env=None):
        out = io.StringIO()
        with (
            mock.patch.dict("os.environ", env or {}, clear=True),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(out),
        ):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_version(self):
        self.assertEqual(self.run_cli("--version"), (0, f"power-watch {__version__}\n"))

    def test_a_config_error_exits_2_and_names_the_variable(self):
        with self.assertLogs("power_watch", "ERROR") as logs:
            code, _ = self.run_cli(env={"POWER_WATCH_OFF_AFTER": "x"})
        self.assertEqual(code, 2)
        self.assertIn("POWER_WATCH_OFF_AFTER", logs.output[0])

    def test_no_device_exits_2(self):
        with self.assertLogs("power_watch", "ERROR"):
            self.assertEqual(self.run_cli()[0], 2)

    def test_scan_refuses_huge_networks(self):
        self.assertEqual(self.run_cli("--scan", "10.0.0.0/8")[0], 2)

    def test_telegram_commands_need_a_token(self):
        self.assertEqual(self.run_cli("--test-notify")[0], 2)
        self.assertEqual(self.run_cli("--find-chats")[0], 2)


if __name__ == "__main__":
    unittest.main()
