"""cmux is the default surface, so the product has to set it up.

"Installed for everyone" was not true: setup printed brew commands and
exited, which on a machine that had never seen cmux meant the product did
not start at all. Three separate things have to be true, and each one was
a real failure here:

  - cmux is installed;
  - cmux is RUNNING, because its control socket exists only while the app
    does;
  - cmux is configured to accept socket control, which lives in the
    user's own config file - and which cmux rewrote from a template
    mid-session, dropping the password and stopping the app booting.

Run with:  python3 -m unittest tests.test_cmux_install -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import cmux_setup


class ConfiguringAccess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "cmux.json"
        self.secret = Path(self.tmp.name) / ".password"

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self):
        return cmux_setup.ensure_socket_access(self.config, self.secret)

    def test_a_machine_with_no_cmux_config_gets_one(self):
        password = self.configure()
        written = json.loads(self.config.read_text())
        self.assertEqual(written["automation"]["socketControlMode"],
                         "password")
        self.assertEqual(written["automation"]["socketPassword"], password)

    def test_the_password_is_left_where_the_runtime_looks(self):
        """Otherwise it only works for someone who exports an environment
        variable by hand - which is not "everyone"."""
        password = self.configure()
        self.assertEqual(self.secret.read_text().strip(), password)
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o600)

    def test_existing_settings_are_kept(self):
        self.config.write_text(json.dumps(
            {"app": {"minimalMode": True},
             "automation": {"portBase": 9100}}))
        self.configure()
        written = json.loads(self.config.read_text())
        self.assertTrue(written["app"]["minimalMode"])
        self.assertEqual(written["automation"]["portBase"], 9100)

    def test_a_config_with_comments_is_still_read(self):
        """cmux writes JSON with comments; parsing it as strict JSON
        would have thrown away every setting the user had."""
        self.config.write_text(
            '{\n  // cmux writes these\n  "app": {"minimalMode": true},\n'
            '  "automation": {"portBase": 9100}\n}\n')
        self.configure()
        written = json.loads(self.config.read_text())
        self.assertTrue(written["app"]["minimalMode"])

    def test_an_existing_password_is_not_churned(self):
        """Rotating it would lock out anything already holding it."""
        first = self.configure()
        self.assertEqual(self.configure(), first)

    def test_a_password_cmux_dropped_is_restored_not_replaced(self):
        """Exactly what happened here: cmux rewrote its config from a
        template, keeping socketControlMode: password and losing
        socketPassword, and the app would not boot."""
        original = self.configure()
        self.config.write_text(json.dumps(
            {"automation": {"socketControlMode": "password"}}))
        self.assertEqual(self.configure(), original,
                         "invented a new password instead of restoring")

    def test_the_previous_config_is_backed_up_before_writing(self):
        self.config.write_text(json.dumps({"app": {"minimalMode": True}}))
        self.configure()
        backups = list(Path(self.tmp.name).glob("*.bak"))
        self.assertTrue(backups, "overwrote the user's config with no copy")


class InstallingIt(unittest.TestCase):
    def test_without_homebrew_it_explains_rather_than_guessing(self):
        """Downloading and mounting a DMG ourselves would put software on
        the machine that the user cannot update or remove by the usual
        means."""
        with mock.patch.object(cmux_setup.shutil, "which", return_value=None), \
             mock.patch.object(cmux_setup.Path, "exists", return_value=False):
            ok, detail = asyncio.run(cmux_setup.install(announce=lambda *a: None))
        self.assertFalse(ok)
        self.assertIn("Homebrew", detail)
        self.assertIn("brew install --cask cmux", detail)

    def test_brew_is_found_in_either_homebrew_prefix(self):
        """Apple Silicon keeps it in /opt/homebrew and Intel in
        /usr/local. Hardcoding one refused to install on a machine that
        had the other - including the one this was written on."""
        for prefix in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
            calls = []

            async def fake_run(*args, **kwargs):
                calls.append(args)
                return 0, "ok"
            with mock.patch.object(cmux_setup.shutil, "which",
                                   return_value=None), \
                 mock.patch.object(cmux_setup.Path, "exists",
                                   lambda self: str(self) == prefix), \
                 mock.patch.object(cmux_setup, "_run", fake_run):
                ok, detail = asyncio.run(
                    cmux_setup.install(announce=lambda *a: None))
            self.assertTrue(ok, f"{prefix}: {detail}")
            self.assertEqual(calls[0][0], prefix)

    def test_it_installs_the_cask(self):
        calls = []

        async def fake_run(*args, **kwargs):
            calls.append(args)
            return 0, "ok"
        with mock.patch.object(cmux_setup.shutil, "which",
                               return_value="/opt/homebrew/bin/brew"), \
             mock.patch.object(cmux_setup, "_run", fake_run):
            ok, _ = asyncio.run(cmux_setup.install(announce=lambda *a: None))
        self.assertTrue(ok)
        self.assertIn(cmux_setup.BREW_TAP, calls[0])
        self.assertIn("--cask", calls[1])

    def test_an_already_installed_cask_is_not_a_failure(self):
        """Re-running setup on a machine that already has cmux must be a
        no-op, not a startup failure."""
        async def fake_run(*args, **kwargs):
            if "tap" in args:
                return 0, ""
            return 1, "Warning: Cask 'cmux' is already installed."
        with mock.patch.object(cmux_setup.shutil, "which",
                               return_value="/opt/homebrew/bin/brew"), \
             mock.patch.object(cmux_setup, "_run", fake_run):
            ok, _ = asyncio.run(cmux_setup.install(announce=lambda *a: None))
        self.assertTrue(ok)


class StartingIt(unittest.TestCase):
    def test_an_installed_but_stopped_cmux_is_started(self):
        """The control socket exists only while the app runs, so an
        installed cmux is still an unreachable one."""
        opened = []
        answers = [False, True]

        async def fake_run(*args, **kwargs):
            opened.append(args)
            return 0, ""
        with mock.patch.object(cmux_setup, "_run", fake_run), \
             mock.patch.object(cmux_setup.asyncio, "sleep",
                               mock.AsyncMock()), \
             mock.patch.object(cmux_setup.asyncio,
                               "create_subprocess_exec") as spawn:
            proc = mock.AsyncMock()
            proc.communicate = mock.AsyncMock(
                side_effect=[(b"", b""), (b"PONG", b"")])
            proc.returncode = 0
            spawn.return_value = proc
            started = asyncio.run(cmux_setup.ensure_running("/bin/cmux", "pw"))
        self.assertTrue(started)
        self.assertTrue(any("open" in a for a in opened),
                        "never tried to launch cmux")


if __name__ == "__main__":
    unittest.main()
