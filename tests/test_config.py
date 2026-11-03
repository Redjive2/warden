import os
import tempfile
import unittest

from warden.config import Config, ConfigError, parse, read_token


def minimal(**extra):
    d = {
        "token_file": "~/.warden/token",
        "guild": {"id": 10, "report_channel": 11},
        "rules": {"evil_role": 12, "honeypot_channel": 13},
    }
    for k, v in extra.items():
        d[k] = v
    return d


class ParseTest(unittest.TestCase):
    def test_minimal_config_defaults_to_dry_run(self):
        cfg = parse(minimal())
        self.assertTrue(cfg.dry_run)
        self.assertEqual(cfg.delete_message_seconds, 3600)
        self.assertEqual(cfg.guild_id, 10)
        self.assertEqual(cfg.staff_role_ids, frozenset())

    def test_full_config(self):
        cfg = parse(minimal(
            dry_run=False,
            exempt={"staff_roles": [20, 21], "users": [30], "bots": [40]},
            report={"batch_seconds": 5},
            paths={"state_file": "/tmp/s.json", "audit_log": "/tmp/a.jsonl"},
        ))
        self.assertFalse(cfg.dry_run)
        self.assertEqual(cfg.staff_role_ids, frozenset({20, 21}))
        self.assertEqual(cfg.exempt_user_ids, frozenset({30}))
        self.assertEqual(cfg.exempt_bot_ids, frozenset({40}))
        self.assertEqual(cfg.batch_seconds, 5.0)

    def assertRefused(self, data, fragment):
        with self.assertRaises(ConfigError) as ctx:
            parse(data)
        self.assertIn(fragment, str(ctx.exception))

    def test_unknown_key_section_and_subkey_stop_it(self):
        self.assertRefused(minimal(dryrun=True), "unknown key dryrun")
        self.assertRefused(minimal(extra={"x": 1}), "unknown section [extra]")
        d = minimal()
        d["rules"]["evil_roles"] = 5
        self.assertRefused(d, "unknown key rules.evil_roles")

    def test_missing_required(self):
        d = minimal()
        del d["rules"]["honeypot_channel"]
        self.assertRefused(d, "missing required key rules.honeypot_channel")
        d = minimal()
        del d["token_file"]
        self.assertRefused(d, "missing required key token_file")

    def test_ids_must_be_positive_integers_not_bools_or_strings(self):
        for bad in (True, "13", 0, -5, 1.5):
            d = minimal()
            d["rules"]["honeypot_channel"] = bad
            self.assertRefused(d, "rules.honeypot_channel must be a Discord id")
        d = minimal(exempt={"users": [1, "2"]})
        self.assertRefused(d, "exempt.users[1]")

    def test_dry_run_must_be_a_bool(self):
        self.assertRefused(minimal(dry_run="false"), "dry_run must be true or false")

    def test_token_in_config_is_refused_anywhere(self):
        self.assertRefused(minimal(token="abc"), "never goes in the config")
        d = minimal()
        d["guild"]["bot_token"] = "abc"
        self.assertRefused(d, "guild.bot_token")

    def test_delete_seconds_within_discords_limit(self):
        for ok in (0, 3600, 604800):
            d = minimal()
            d["rules"]["delete_message_seconds"] = ok
            self.assertEqual(parse(d).delete_message_seconds, ok)
        for bad in (-1, 604801, True):
            d = minimal()
            d["rules"]["delete_message_seconds"] = bad
            self.assertRefused(d, "delete_message_seconds")

    def test_honeypot_cannot_be_the_report_channel(self):
        d = minimal()
        d["rules"]["honeypot_channel"] = 11
        self.assertRefused(d, "same channel")

    def test_evil_role_cannot_be_a_staff_role(self):
        self.assertRefused(minimal(exempt={"staff_roles": [12]}), "also listed in exempt.staff_roles")


class TokenTest(unittest.TestCase):
    def _cfg(self, path):
        return Config(guild_id=1, report_channel_id=2, evil_role_id=3, honeypot_channel_id=4, token_file=path)

    def test_token_file_must_be_owner_only(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "token")
        with open(path, "w") as f:
            f.write("secret-token\n")
        os.chmod(path, 0o644)
        with self.assertRaises(ConfigError) as ctx:
            read_token(self._cfg(path))
        self.assertIn("chmod 600", str(ctx.exception))
        os.chmod(path, 0o600)
        self.assertEqual(read_token(self._cfg(path)), "secret-token")

    def test_missing_and_empty_token_file(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(ConfigError):
            read_token(self._cfg(os.path.join(d, "nope")))
        path = os.path.join(d, "empty")
        open(path, "w").close()
        os.chmod(path, 0o600)
        with self.assertRaises(ConfigError) as ctx:
            read_token(self._cfg(path))
        self.assertIn("empty", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
