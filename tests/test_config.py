import unittest

from power_watch.config import ConfigError, from_env


class Config(unittest.TestCase):
    def test_defaults(self):
        cfg = from_env({"POWER_WATCH_DEVICE_IP": "192.168.0.150"})
        self.assertEqual(
            (cfg.device, cfg.lang, cfg.interval, cfg.off_after, cfg.gw_port, cfg.allowed),
            ("192.168.0.150", "en", 15.0, 4, 53, frozenset()),
        )

    def test_lists_tokens_and_whitespace(self):
        self.assertEqual(from_env({"POWER_WATCH_ALLOWED_USERS": " 7, 42 8 "}).allowed, frozenset({7, 42, 8}))
        cfg = from_env({"POWER_WATCH_PURIFIER_IP": "192.168.0.155", "POWER_WATCH_PURIFIER_TOKEN": "AB" * 16})
        self.assertEqual((cfg.purifier_ip, cfg.purifier_token), ("192.168.0.155", "ab" * 16))

    def test_bad_values_are_config_errors(self):
        for env in (
            {"POWER_WATCH_OFF_AFTER": "4   # misses"},
            {"POWER_WATCH_OFF_AFTER": "0"},
            {"POWER_WATCH_INTERVAL_S": "0"},
            {"POWER_WATCH_TZ": "Europe/Kyiv  # x"},
            {"POWER_WATCH_TZ": "../etc"},
            {"POWER_WATCH_LANG": "de"},
            {"POWER_WATCH_DEVICE_IP": "192.168.0.150 # lamp"},
            {"POWER_WATCH_GATEWAY": "router"},
            {"POWER_WATCH_ALLOWED_USERS": "@me"},
            {"POWER_WATCH_PURIFIER_TOKEN": "abc"},
            {"POWER_WATCH_PURIFIER_TOKEN": "z" * 32},
            {"POWER_WATCH_PURIFIER_IP": "purifier"},
        ):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                from_env(env)

    def test_errors_never_echo_the_purifier_token(self):
        token = "q" * 32
        with self.assertRaises(ConfigError) as e:
            from_env({"POWER_WATCH_PURIFIER_TOKEN": token})
        self.assertNotIn(token, str(e.exception))


if __name__ == "__main__":
    unittest.main()
