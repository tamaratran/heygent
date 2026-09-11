"""What the app does when it is opened, before conduct.py has the floor."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import app_launch, app_setup

GOOD = "sk-good-0123456789abcdef"


class TheEnvironment(unittest.TestCase):
    def test_the_users_tools_come_first_and_the_apps_own_last(self):
        path = app_launch.app_path("/usr/bin:/bin:/usr/sbin:/sbin",
                                   Path("/Users/me"),
                                   Path("/A.app/Contents/Resources/bin"))
        parts = path.split(":")
        self.assertEqual(parts[0], "/Users/me/.local/bin")
        self.assertIn("/opt/homebrew/bin", parts)
        self.assertLess(parts.index("/usr/local/bin"), parts.index("/usr/bin"))
        self.assertEqual(parts[-1], "/A.app/Contents/Resources/bin")
        self.assertEqual(len(parts), len(set(parts)))

    def test_a_terminals_path_is_kept(self):
        path = app_launch.app_path("/Users/me/bin:/usr/bin", Path("/Users/me"),
                                   None)
        self.assertIn("/Users/me/bin", path.split(":"))

    def test_claude_code_markers_and_a_stale_anthropic_key_are_dropped(self):
        env = app_launch.prepare_environment(
            {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "x",
             "CLAUDE_CODE_CHILD_SESSION": "1", "KEEP": "me"},
            Path("/Users/me"), None)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", env)
        self.assertEqual(env["KEEP"], "me")
        self.assertEqual(env["LANG"], "en_US.UTF-8")

    def test_the_home_is_read_like_conduct_py_reads_it(self):
        self.assertEqual(app_launch.conductor_home(["--home", "/h"]), Path("/h"))
        self.assertEqual(app_launch.conductor_home(["--home=/h2"]), Path("/h2"))
        self.assertEqual(app_launch.conductor_home([]),
                         app_launch.DEFAULT_HOME)


class Opening(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.home = Path(self.scratch.name)
        self.calls = []
        self.window_code = 0
        self.conduct = mock.Mock()
        patches = [
            mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False),
            mock.patch.object(app_launch, "log_output"),
            mock.patch.object(app_launch.os, "chdir"),
            mock.patch.object(app_launch.runpy, "run_path", self.conduct),
            mock.patch.object(app_launch.instance, "read", return_value=None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("OPENAI_API_KEY", None)

    def run_process(self, argv, **kwargs):
        self.calls.append(argv)
        if "--home" in argv and "--alert" not in argv:   # the setup window
            if self.window_code == 0:
                app_setup.save_key(self.home, GOOD)
                app_setup.mark_completed(self.home, {})
        return subprocess.CompletedProcess(argv, self.window_code, "", "")

    def open(self, *extra):
        return app_launch.main(["--home", str(self.home), *extra],
                               run=self.run_process)

    def windows(self):
        return [argv for argv in self.calls
                if "conductor.app_setup_window" in argv and "--alert" not in argv]

    def alerts(self):
        return [argv for argv in self.calls if "--alert" in argv]

    def test_the_first_run_shows_setup_then_starts_with_the_saved_key(self):
        seen = {}
        self.conduct.side_effect = lambda path, run_name: seen.update(
            key=os.environ.get("OPENAI_API_KEY"), argv=list(app_launch.sys.argv))
        self.assertEqual(self.open(), 0)
        self.assertEqual(len(self.windows()), 1)
        self.assertEqual(seen["key"], GOOD)
        self.assertTrue(seen["argv"][0].endswith("conduct.py"))
        self.assertEqual(seen["argv"][1:], ["--home", str(self.home)])

    def test_closing_setup_starts_nothing(self):
        self.window_code = 1
        self.assertEqual(self.open(), 0)
        self.conduct.assert_not_called()

    def test_a_window_that_exits_zero_without_finishing_starts_nothing(self):
        """NSApplication's terminate: exits 0; the files are the truth."""
        def run(argv, **kwargs):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        app_launch.main(["--home", str(self.home)], run=run)
        self.conduct.assert_not_called()

    def test_once_set_up_it_starts_straight_away(self):
        app_setup.save_key(self.home, GOOD)
        app_setup.mark_completed(self.home, {})
        self.open()
        self.assertEqual(self.windows(), [])
        self.conduct.assert_called_once()

    def test_setup_can_be_asked_for_again(self):
        app_setup.save_key(self.home, GOOD)
        app_setup.mark_completed(self.home, {})
        seen = {}
        self.conduct.side_effect = lambda path, run_name: seen.update(
            argv=list(app_launch.sys.argv))
        self.open("--setup")
        self.assertEqual(len(self.windows()), 1)
        self.assertNotIn("--setup", seen["argv"])

    def test_a_running_conductor_is_said_in_an_alert(self):
        running = mock.Mock(pid=4242)
        with mock.patch.object(app_launch.instance, "read",
                               return_value=running), \
             mock.patch.object(app_launch.instance, "alive",
                               return_value=True):
            self.assertEqual(self.open(), 0)
        self.assertEqual(self.windows(), [])
        self.conduct.assert_not_called()
        [alert] = self.alerts()
        self.assertIn("already running", alert[alert.index("--alert") + 1])
        self.assertIn("4242", alert[alert.index("--detail") + 1])

    def test_a_conductor_that_exits_with_an_error_is_said_in_an_alert(self):
        app_setup.save_key(self.home, GOOD)
        app_setup.mark_completed(self.home, {})
        (self.home / "logs").mkdir()
        app_launch.log_path(self.home).write_text("tmux not found\n")
        self.conduct.side_effect = SystemExit(1)
        self.assertEqual(self.open(), 1)
        [alert] = self.alerts()
        self.assertIn("tmux not found", alert[alert.index("--detail") + 1])

    def test_a_clean_exit_is_quiet(self):
        app_setup.save_key(self.home, GOOD)
        app_setup.mark_completed(self.home, {})
        self.conduct.side_effect = SystemExit(0)
        self.assertEqual(self.open(), 0)
        self.assertEqual(self.alerts(), [])

    def test_a_terminals_own_key_wins(self):
        app_setup.save_key(self.home, GOOD)
        app_setup.mark_completed(self.home, {})
        seen = {}
        self.conduct.side_effect = lambda path, run_name: seen.update(
            key=os.environ.get("OPENAI_API_KEY"))
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-from-shell"}):
            self.open()
        self.assertEqual(seen["key"], "sk-from-shell")


if __name__ == "__main__":
    unittest.main()
