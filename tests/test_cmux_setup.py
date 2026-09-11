"""cmux as a declared dependency, checked at startup.

The end state the spec asks for: a fresh install has a deterministic path
to a working cmux, and nobody is expected to have known in advance that
they were supposed to install it.

Two failures these guard against. Resolving cmux through $PATH, which is
not a dependency contract - a launchd context, another user, or a stale
shell all have different ones. And degrading quietly when cmux is
unusable, which would leave workers running somewhere the user is not
looking; from the outside that is indistinguishable from nothing
happening.

Run with:  python3 -m unittest tests.test_cmux_setup -v
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from conductor import cmux_setup
from conductor.cmux_setup import (MIN_VERSION, REQUIRED, CmuxDependency,
                                  find_executable, parse_version)


class ResolvingTheExecutable(unittest.TestCase):
    def test_the_app_bundle_beats_path(self):
        """/usr/local/bin/cmux is a symlink INTO the bundle, so the bundle
        is the real location; $PATH is a convenience that may be absent."""
        bundle = cmux_setup.KNOWN_PATHS[0]
        with mock.patch.object(Path, "exists",
                               lambda self: self == bundle), \
             mock.patch("shutil.which", return_value="/somewhere/else/cmux"), \
             mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(find_executable(), str(bundle))

    def test_an_explicit_override_wins(self):
        """For packaging and for tests."""
        with mock.patch.dict("os.environ", {"CMUX_BINARY": "/opt/mine/cmux"}), \
             mock.patch.object(Path, "exists", lambda self: True):
            self.assertEqual(find_executable(), "/opt/mine/cmux")

    def test_an_override_that_does_not_exist_is_ignored(self):
        with mock.patch.dict("os.environ", {"CMUX_BINARY": "/nope/cmux"}), \
             mock.patch.object(Path, "exists", lambda self: False), \
             mock.patch("shutil.which", return_value=None):
            self.assertIsNone(find_executable())

    def test_path_is_the_last_resort_not_the_first(self):
        with mock.patch.object(Path, "exists", lambda self: False), \
             mock.patch("shutil.which", return_value="/usr/bin/cmux"), \
             mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(find_executable(), "/usr/bin/cmux")

    def test_nothing_anywhere_is_not_installed(self):
        with mock.patch.object(Path, "exists", lambda self: False), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(find_executable())


class ReadingTheVersion(unittest.TestCase):
    def test_the_real_output_parses(self):
        self.assertEqual(
            parse_version("cmux 0.64.22 (102) [ddd4a01bc]"), (0, 64, 22))

    def test_nonsense_is_no_version_rather_than_a_guess(self):
        self.assertIsNone(parse_version("command not found"))
        self.assertIsNone(parse_version(""))

    def test_the_floor_is_the_version_we_actually_proved(self):
        self.assertEqual(parse_version(cmux_setup.VERIFIED_VERSION),
                         (0, 64, 22))
        self.assertLessEqual(MIN_VERSION, (0, 64, 22))


class TheStartupAnswer(unittest.IsolatedAsyncioTestCase):
    def runner(self, version="cmux 0.64.22", ping="PONG",
               help_text=" ".join(REQUIRED), codes=(0, 0, 0)):
        calls = iter([(codes[0], version), (codes[1], ping),
                      (codes[2], help_text)])

        async def fake(*args, **kwargs):
            proc = mock.AsyncMock()
            code, text = next(calls)
            proc.returncode = code
            proc.communicate = mock.AsyncMock(
                return_value=(text.encode(), b""))
            return proc
        return fake

    async def inspect_with(self, **kw) -> CmuxDependency:
        with mock.patch("conductor.cmux_setup.find_executable",
                        return_value="/Applications/cmux.app/…/cmux"), \
             mock.patch("asyncio.create_subprocess_exec",
                        side_effect=self.runner(**kw)):
            return await cmux_setup.inspect()

    async def test_a_good_install_is_usable(self):
        state = await self.inspect_with()
        self.assertTrue(state.usable)
        self.assertEqual(state.version, "0.64.22")
        self.assertEqual(state.missing, [])

    async def test_not_installed_is_not_usable_and_says_how(self):
        with mock.patch("conductor.cmux_setup.find_executable",
                        return_value=None):
            state = await cmux_setup.inspect()
        self.assertFalse(state.installed)
        self.assertFalse(state.usable)
        self.assertIn("brew install --cask cmux", state.explain())

    async def test_an_old_version_is_a_setup_error_not_a_fallback(self):
        state = await self.inspect_with(version="cmux 0.1.0")
        self.assertTrue(state.installed)
        self.assertFalse(state.compatible)
        self.assertFalse(state.usable)
        self.assertIn("too old", state.explain())
        self.assertIn("brew upgrade", state.explain())

    async def test_installed_but_socket_silent_is_not_usable(self):
        """The socket exists only while cmux runs, so this is the ordinary
        case of the app being closed - and it must not read as ready."""
        state = await self.inspect_with(ping="Socket not found", codes=(0, 1, 0))
        self.assertTrue(state.installed)
        self.assertFalse(state.socket_available)
        self.assertFalse(state.usable)
        self.assertIn("only while the app runs", state.explain())

    async def test_access_denied_is_reported_with_the_setting_to_change(self):
        state = await self.inspect_with(
            ping="ERROR: Access denied - only processes started inside cmux",
            codes=(0, 1, 0))
        self.assertFalse(state.usable)
        self.assertIn("socketControlMode", state.explain())

    async def test_missing_commands_are_named(self):
        state = await self.inspect_with(help_text="ping list-workspaces")
        self.assertTrue(state.socket_available)
        self.assertFalse(state.capabilities_ok)
        self.assertIn("send", state.missing)
        self.assertIn("send", state.explain())

    async def test_every_required_command_is_one_the_surface_needs(self):
        """The list is what a Managed Subagent surface actually does:
        create, list, select, focus, send, inspect."""
        for name in ("ping", "new-workspace", "list-workspaces",
                     "select-workspace", "focus-pane", "send",
                     "list-pane-surfaces"):
            self.assertIn(name, REQUIRED)

    async def test_the_shape_matches_the_declared_dependency_state(self):
        state = await self.inspect_with()
        for key in ("installed", "version", "compatible", "executablePath",
                    "socketAvailable"):
            self.assertIn(key, state.as_dict())


class StartupRefusesRatherThanDegrades(unittest.IsolatedAsyncioTestCase):
    def _unusable(self):
        return (mock.patch("conductor.cmux_setup.find_executable",
                           return_value="/Applications/cmux.app/x/cmux"),
                mock.patch("conductor.cmux_setup.repair", return_value=""),
                mock.patch("conductor.cmux_setup.inspect",
                           new=mock.AsyncMock(
                               return_value=CmuxDependency())))

    async def test_it_stops_when_cmux_is_asked_for_by_name_and_unusable(self):
        """Section 8: never silently ignore an explicit choice of where
        the coding agents run."""
        import conduct
        find, repair, inspect = self._unusable()
        with find, repair, inspect, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                await conduct.preflight_surface("cmux")
        self.assertEqual(caught.exception.code, 2)

    async def test_the_default_falls_back_to_tmux_and_says_why(self):
        """Section 8 is about visibility, and a tmux pane is still a
        window the user can open and watch - so the default surface
        being broken degrades loudly instead of stopping the launch."""
        import boss
        import conduct
        find, repair, inspect = self._unusable()
        stderr = io.StringIO()
        with mock.patch.object(boss, "WORKER_SURFACE", "cmux"), \
                find, repair, inspect, contextlib.redirect_stderr(stderr):
            surface = await conduct.preflight_surface()
        self.assertEqual(surface, "terminal")
        self.assertIn("tmux panes", stderr.getvalue())
        self.assertIn("cmux", stderr.getvalue())

    async def test_it_says_nothing_when_cmux_is_not_selected(self):
        """A dependency nothing uses is not a dependency; failing startup
        over it would be theatre."""
        import boss
        import conduct
        with mock.patch.object(boss, "WORKER_SURFACE", "terminal"), \
             mock.patch("conductor.cmux_setup.inspect",
                        new=mock.AsyncMock()) as looked:
            await conduct.preflight_surface()
        looked.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
