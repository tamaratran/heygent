"""cmux breaks its own configuration, so we have to fix it as we go.

Two things cmux does that nothing else in the product does:

  - it REWRITES ~/.config/cmux/cmux.json from a template every time it
    launches, and the rewrite drops socketPassword while keeping
    socketControlMode: password. The app is then running, reachable, and
    refusing every command with "Password mode is enabled but no socket
    password is configured in Settings".
  - it is an app, so the user can close it, taking every worker with it.

The first one made setup write a password and then launch the app that
erased it - "surface: cmux ready" at startup, and every later command
failing. Both are fixable without the user restarting anything.

Run with:  python3 -m unittest tests.test_cmux_self_repair -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from conductor import cmux_setup
from conductor.cmux_setup import needs_repair, unavailable_reason

LOST_PASSWORD = ("Error: ERROR: Password mode is enabled but no socket "
                 "password is configured in Settings.")
CLOSED = ("Error: Failed to connect to socket at "
          "/Users/x/.local/state/cmux/cmux.sock (Connection refused, "
          "errno 61)")


class RecognisingWhatWentWrong(unittest.TestCase):
    def test_a_config_cmux_rewrote_is_repairable(self):
        self.assertTrue(needs_repair(LOST_PASSWORD))
        self.assertIn("password", unavailable_reason(LOST_PASSWORD))

    def test_a_closed_cmux_is_repairable(self):
        self.assertTrue(needs_repair(CLOSED))

    def test_an_ordinary_rejection_is_not(self):
        """Repairing on every failure would relaunch the app because an
        argument was wrong."""
        self.assertFalse(needs_repair(
            "Error: invalid_params: Missing or invalid group_id"))

    def test_access_denied_is_not_repaired_blindly(self):
        """A user who set socketControlMode deliberately is not having it
        overwritten on a failed command."""
        self.assertFalse(needs_repair("Error: Access denied"))


class TheSurfaceFixesItselfAndRetries(unittest.TestCase):
    def surface(self, failures):
        from conductor.cmux_surface import CmuxSurface
        s = CmuxSurface(binary="/fake/cmux")
        self.attempts = []

        def run(argv, **kwargs):
            self.attempts.append(argv)
            if len(self.attempts) <= failures:
                return mock.Mock(returncode=1, stdout="", stderr=LOST_PASSWORD)
            return mock.Mock(returncode=0, stdout="OK", stderr="")
        self.patched = mock.patch("subprocess.run", run)
        return s

    def test_a_lost_password_is_written_back_and_the_command_retried(self):
        s = self.surface(failures=1)
        with self.patched, mock.patch("conductor.cmux_surface.repair",
                        return_value="new-password") as fixed:
            self.assertEqual(s._run("select-workspace", "--workspace", "w"), "OK")
        fixed.assert_called_once()
        self.assertEqual(len(self.attempts), 2, "did not retry after fixing")

    def test_it_does_not_retry_for_ever(self):
        """A cmux that cannot be repaired must surface as an error, not a
        loop that never returns."""
        from conductor.cmux_surface import CmuxUnavailableError
        s = self.surface(failures=99)
        with self.patched, mock.patch("conductor.cmux_surface.repair", return_value=""):
            with self.assertRaises(CmuxUnavailableError):
                s._run("select-workspace", "--workspace", "w")
        self.assertEqual(len(self.attempts), 2, "retried more than once")


class TheRuntimeFixesItselfToo(unittest.TestCase):
    def runtime(self, failures):
        from conductor.cmux_runtime import CmuxClaudeRuntime
        rt = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
        rt.cmux = "/fake/cmux"
        rt._password = "old"
        rt.places = {}
        rt.transcript = None
        self.attempts = []

        def run(argv, **kwargs):
            self.attempts.append(argv)
            if len(self.attempts) <= failures:
                return mock.Mock(returncode=1, stdout="", stderr=CLOSED)
            return mock.Mock(returncode=0, stdout="OK", stderr="")
        self.patched = mock.patch("subprocess.run", run)
        return rt

    def test_a_closed_cmux_is_reopened_and_the_command_retried(self):
        rt = self.runtime(failures=1)
        with self.patched, mock.patch("conductor.cmux_runtime.repair", return_value="pw"):
            out = rt._cmux("send", "--surface", "s", "hi")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(len(self.attempts), 2)

    def test_the_repaired_password_is_kept_for_next_time(self):
        rt = self.runtime(failures=1)
        with self.patched, mock.patch("conductor.cmux_runtime.repair",
                        return_value="fresh-password"):
            rt._cmux("send", "--surface", "s", "hi")
        self.assertEqual(rt._password, "fresh-password")

    def test_a_repair_that_fails_still_returns_a_result(self):
        """_cmux promises a _Result to every caller and they read .stdout
        off it without looking - `self._cmux("list-workspaces").stdout` is
        the shape of every call site. repair() launches an app and writes
        config files, so it can raise; raising through here turns a cmux
        that is merely closed into an exception out of create_session."""
        import subprocess

        for boom in (OSError("no such file"),
                     subprocess.TimeoutExpired("open", 30)):
            with self.subTest(raised=type(boom).__name__):
                rt = self.runtime(failures=1)
                with self.patched, \
                     mock.patch("conductor.cmux_runtime.repair",
                                side_effect=boom):
                    out = rt._cmux("send", "--surface", "s", "hi")
                self.assertEqual(out.returncode, 1)
                self.assertEqual(out.stdout, "")

    def test_a_failed_repair_reports_the_commands_own_failure(self):
        """Not the repair's. The caller asked whether the command worked,
        and cmux's own stderr is the part that says why."""
        rt = self.runtime(failures=1)
        with self.patched, \
             mock.patch("conductor.cmux_runtime.repair",
                        side_effect=OSError("open: not found")):
            out = rt._cmux("send", "--surface", "s", "hi")
        self.assertEqual(out.stderr, CLOSED)
        self.assertNotIn("open: not found", out.stderr)

    def test_a_failed_repair_does_not_retry(self):
        """A repair that raised is a repair that did not happen; retrying
        against the same broken cmux just fails twice as slowly."""
        rt = self.runtime(failures=1)
        with self.patched, \
             mock.patch("conductor.cmux_runtime.repair",
                        side_effect=OSError("boom")):
            rt._cmux("send", "--surface", "s", "hi")
        self.assertEqual(len(self.attempts), 1)

    def test_a_failed_repair_keeps_the_password_it_had(self):
        rt = self.runtime(failures=1)
        with self.patched, \
             mock.patch("conductor.cmux_runtime.repair",
                        side_effect=OSError("boom")):
            rt._cmux("send", "--surface", "s", "hi")
        self.assertEqual(rt._password, "old")


class SurvivingCmuxRewritingItsOwnConfig(unittest.TestCase):
    """The bug this file exists for. cmux rewrites its config from a
    template as it launches, dropping the password we just wrote. Writing
    once and hoping loses that race: startup reported "cmux ready" and
    every later command failed against an app that looked healthy."""

    def test_the_password_is_rewritten_until_cmux_accepts_it(self):
        writes = []
        pings = [False, False, True]        # cmux clobbers us twice

        def configure(*a, **k):
            writes.append(1)
            return "pw"

        with mock.patch.object(cmux_setup, "is_running", lambda: True), \
             mock.patch.object(cmux_setup, "ensure_socket_access", configure), \
             mock.patch.object(cmux_setup, "_ping_sync",
                               lambda b, p: pings.pop(0)), \
             mock.patch.object(cmux_setup.time, "sleep", lambda s: None):
            password = cmux_setup.repair("/fake/cmux")
        self.assertEqual(password, "pw")
        self.assertEqual(len(writes), 3,
                         "wrote the password once and lost the race")

    def test_a_closed_cmux_is_launched_before_configuring(self):
        launched = []
        states = [False, True]

        with mock.patch.object(cmux_setup, "is_running",
                               lambda: states.pop(0) if states else True), \
             mock.patch.object(cmux_setup.subprocess, "run",
                               lambda *a, **k: launched.append(a)), \
             mock.patch.object(cmux_setup, "ensure_socket_access",
                               lambda *a, **k: "pw"), \
             mock.patch.object(cmux_setup, "_ping_sync", lambda b, p: True), \
             mock.patch.object(cmux_setup.time, "sleep", lambda s: None):
            cmux_setup.repair("/fake/cmux")
        self.assertTrue(launched, "never launched a closed cmux")
        self.assertIn("open", launched[0][0])

    def test_it_gives_up_rather_than_hanging_for_ever(self):
        with mock.patch.object(cmux_setup, "is_running", lambda: True), \
             mock.patch.object(cmux_setup, "ensure_socket_access",
                               lambda *a, **k: "pw"), \
             mock.patch.object(cmux_setup, "_ping_sync", lambda b, p: False), \
             mock.patch.object(cmux_setup.time, "sleep", lambda s: None):
            self.assertEqual(
                cmux_setup.repair("/fake/cmux", timeout=0.2), "pw")


if __name__ == "__main__":
    unittest.main()
