"""The two login failures a person can fix get a sentence, not a traceback."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import discord

from warden import __main__ as cli


def write_config():
    d = tempfile.mkdtemp()
    token = os.path.join(d, "token")
    with open(token, "w") as f:
        f.write("a.b.c")
    os.chmod(token, 0o600)
    path = os.path.join(d, "warden.toml")
    with open(path, "w") as f:
        f.write(f'''token_file = "{token}"
[guild]
id = 10
report_channel = 11
[rules]
evil_role = 12
honeypot_channel = 13
[paths]
state_file = "{d}/state.json"
audit_log = "{d}/audit.jsonl"
''')
    return path


class LoginFailureTest(unittest.TestCase):
    def run_with(self, error):
        err = io.StringIO()
        with mock.patch("warden.bot.WardenClient.run", side_effect=error), \
                contextlib.redirect_stderr(err):
            code = cli.main(["--config", write_config()])
        return code, err.getvalue()

    def test_missing_intent_says_which_switch(self):
        code, text = self.run_with(discord.PrivilegedIntentsRequired(None))
        self.assertEqual(code, 3)
        self.assertIn("Server Members Intent", text)
        self.assertNotIn("Traceback", text)

    def test_rejected_token_says_reset_it(self):
        code, text = self.run_with(discord.LoginFailure("Improper token has been passed."))
        self.assertEqual(code, 3)
        self.assertIn("Reset Token", text)


class LauncherTest(unittest.TestCase):
    def test_bin_warden_runs_under_isolated_mode(self):
        # -I drops the script's directory from sys.path; the launcher must add the
        # release root itself, or every deployed service fails to import warden.
        import subprocess
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, "-I", os.path.join(root, "bin", "warden"), "--help"],
                             capture_output=True, text=True, cwd="/")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--service", out.stdout)


class ServiceModeTest(unittest.TestCase):
    """Under --service, a problem a person must fix exits 0 so launchd stops retrying."""

    def run_service(self, error=None, config=None):
        err = io.StringIO()
        patch = mock.patch("warden.bot.WardenClient.run", side_effect=error)
        with patch, contextlib.redirect_stderr(err):
            code = cli.main(["--config", config or write_config(), "--service"])
        return code, err.getvalue()

    def test_login_problems_exit_zero_and_say_why(self):
        for error in (discord.PrivilegedIntentsRequired(None), discord.LoginFailure("bad")):
            code, text = self.run_service(error)
            self.assertEqual(code, 0, error)
            self.assertIn("a person must fix", text)

    def test_a_bad_config_exits_zero_under_service(self):
        code, text = self.run_service(config="/nonexistent/warden.toml")
        self.assertEqual(code, 0)
        self.assertIn("no config at", text)

    def test_a_crash_still_fails_so_launchd_restarts_it(self):
        with self.assertRaises(RuntimeError):
            self.run_service(RuntimeError("boom"))

    def test_by_hand_the_codes_are_unchanged(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--config", "/nonexistent/warden.toml"]), 2)


if __name__ == "__main__":
    unittest.main()
