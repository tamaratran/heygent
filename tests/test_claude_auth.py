"""Claude Code not signed in is said at the start, not discovered later.

install.sh puts the CLI on the Mac and stops; nothing ran `claude auth
login`. The Boss then opened on Claude's sign-in screen, in a tmux window
the user never looked at, while the voice kept promising an answer. Now
`claude auth status` is asked first and the user is told what to run.

Run with:  python3 -m unittest tests.test_claude_auth -v
"""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from conductor import claude_auth, dialogs


def cli(stdout: str, code: int = 0):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, code, stdout=stdout,
                                           stderr="")
    return run


class ReadingTheStatus(unittest.TestCase):
    def test_a_login_is_seen(self):
        run = cli(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
        self.assertIs(claude_auth.is_logged_in(
            {}, run=run, which=lambda n: "/usr/local/bin/claude"), True)

    def test_no_login_is_seen(self):
        run = cli(json.dumps({"loggedIn": False, "authMethod": "none"}), 1)
        self.assertIs(claude_auth.is_logged_in(
            {}, run=run, which=lambda n: "/usr/local/bin/claude"), False)

    def test_a_token_in_the_environment_is_a_login(self):
        """The CLI takes ANTHROPIC_API_KEY over any stored account, and
        .env may set one; no subprocess is needed to know."""
        calls = []
        self.assertIs(claude_auth.is_logged_in(
            {"ANTHROPIC_API_KEY": "sk-ant-x"}, run=lambda *a, **k: calls.append(a),
            which=lambda n: "/x/claude"), True)
        self.assertEqual(calls, [])

    def test_no_cli_or_no_answer_is_unknown_not_missing(self):
        with mock.patch.object(claude_auth, "application_log"):
            self.assertIsNone(claude_auth.is_logged_in(
                {}, run=cli(""), which=lambda n: None))
            self.assertIsNone(claude_auth.is_logged_in(
                {}, run=cli("not json", 2), which=lambda n: "/x/claude"))
            self.assertIsNone(claude_auth.is_logged_in(
                {}, run=cli(json.dumps({"other": 1})),
                which=lambda n: "/x/claude"))

            def missing(cmd, **kwargs):
                raise FileNotFoundError(cmd[0])
            self.assertIsNone(claude_auth.is_logged_in(
                {}, run=missing, which=lambda n: "/x/claude"))


class TellingTheUser(unittest.TestCase):
    def test_signed_in_or_unknown_starts_the_boss(self):
        with mock.patch.object(claude_auth, "application_log"):
            for verdict in (True, None):
                self.assertTrue(claude_auth.ensure_logged_in(
                    {}, status=lambda env: verdict,
                    alert=lambda *a: self.fail("alerted"),
                    open_terminal=lambda: self.fail("opened")))

    def test_from_finder_a_dialog_offers_the_login(self):
        shown = []
        opened = []
        with mock.patch.object(claude_auth, "application_log"), \
             mock.patch.object(dialogs, "has_terminal", return_value=False):
            ok = claude_auth.ensure_logged_in(
                {}, status=lambda env: False,
                alert=lambda text, buttons: shown.append((text, buttons))
                or "Sign in",
                open_terminal=lambda: opened.append(1) or True)
        self.assertFalse(ok)
        text, buttons = shown[0]
        self.assertIn("claude auth login", text)
        self.assertIn("not signed in", text)
        self.assertEqual(buttons, ("Quit", "Sign in"))
        self.assertEqual(opened, [1])

    def test_quit_opens_nothing(self):
        opened = []
        with mock.patch.object(claude_auth, "application_log"), \
             mock.patch.object(dialogs, "has_terminal", return_value=False):
            ok = claude_auth.ensure_logged_in(
                {}, status=lambda env: False,
                alert=lambda text, buttons: "Quit",
                open_terminal=lambda: opened.append(1) or True)
        self.assertFalse(ok)
        self.assertEqual(opened, [])

    def test_on_a_terminal_the_words_are_printed(self):
        told = []
        with mock.patch.object(claude_auth, "application_log"), \
             mock.patch.object(dialogs, "has_terminal", return_value=True), \
             mock.patch.object(dialogs, "tell", told.append):
            ok = claude_auth.ensure_logged_in(
                {}, status=lambda env: False,
                alert=lambda *a: self.fail("a dialog on a terminal"),
                open_terminal=lambda: self.fail("opened"))
        self.assertFalse(ok)
        self.assertIn("claude auth login", told[0])

    def test_the_terminal_gets_the_cli_by_its_full_path(self):
        """A fresh install is in ~/.local/bin, which the user's shell may
        not have on PATH; the command names the binary this process
        found."""
        ran = []

        def run(cmd, **kwargs):
            ran.append((cmd, kwargs.get("input", "")))
            return subprocess.CompletedProcess(cmd, 0)
        self.assertTrue(claude_auth.open_login_terminal(
            run=run, which=lambda n: "/Users/me/.local/bin/claude"))
        cmd, script = ran[0]
        self.assertEqual(cmd[:2], ["osascript", "-"])
        self.assertEqual(cmd[2], "/Users/me/.local/bin/claude auth login")
        self.assertIn('tell application "Terminal"', script)


class Dialogs(unittest.TestCase):
    """The words reach the person: a terminal when there is one, a
    macOS dialog when the app was opened from Finder."""

    def test_tell_prints_and_only_alerts_without_a_terminal(self):
        alerts = []
        with mock.patch.object(dialogs, "has_terminal", return_value=True), \
             mock.patch.object(dialogs, "alert",
                               lambda text, *a, **k: alerts.append(text)), \
             mock.patch.object(dialogs.sys, "stderr") as err:
            dialogs.tell("hello")
        self.assertEqual(alerts, [])
        err.write.assert_called()
        with mock.patch.object(dialogs, "has_terminal", return_value=False), \
             mock.patch.object(dialogs, "alert",
                               lambda text, *a, **k: alerts.append(text)), \
             mock.patch.object(dialogs.sys, "stderr"):
            dialogs.tell("hello")
        self.assertEqual(alerts, ["hello"])

    def test_an_unexplained_stop_is_a_dialog_with_the_log_a_click_away(self):
        """A crash from Finder used to be a traceback in a file nobody
        knew about; now it is said, and Show Log opens that file. On a
        terminal the traceback that follows is the explanation."""
        shown = []
        opened = []
        with mock.patch.object(dialogs, "has_terminal", return_value=False), \
             mock.patch.object(dialogs, "alert",
                               lambda text, buttons, **k:
                               shown.append((text, buttons)) or "Show Log"), \
             mock.patch.object(dialogs, "open_url",
                               lambda url, **k: opened.append(url) or True):
            dialogs.stopped("RuntimeError: no overlay", "/x/app-launch.log")
        (text, buttons), = shown
        self.assertIn("heygent stopped: RuntimeError: no overlay", text)
        self.assertIn("/x/app-launch.log", text)
        self.assertEqual(buttons, ("Quit", "Show Log"))
        self.assertEqual(opened, ["/x/app-launch.log"])

        shown.clear()
        opened.clear()
        with mock.patch.object(dialogs, "has_terminal", return_value=False), \
             mock.patch.object(dialogs, "alert",
                               lambda text, buttons, **k:
                               shown.append(text) or "Quit"), \
             mock.patch.object(dialogs, "open_url",
                               lambda url, **k: opened.append(url) or True):
            dialogs.stopped("boom", None)
        self.assertEqual(len(shown), 1)
        self.assertEqual(opened, [])

        shown.clear()
        with mock.patch.object(dialogs, "has_terminal", return_value=True), \
             mock.patch.object(dialogs, "alert",
                               lambda text, buttons, **k: shown.append(text)):
            dialogs.stopped("boom", "/x/app-launch.log")
        self.assertEqual(shown, [])

    def test_the_text_and_buttons_are_arguments_not_script(self):
        """Nothing the user typed, or an error message quoting it, is
        ever spliced into AppleScript source."""
        ran = []

        def run(cmd, **kwargs):
            ran.append((cmd, kwargs["input"]))
            return subprocess.CompletedProcess(cmd, 0, stdout="Open\n",
                                               stderr="")
        with mock.patch.object(dialogs, "can_show", return_value=True):
            chosen = dialogs.alert('say "hi"', ("Later", "Open"), run=run)
        self.assertEqual(chosen, "Open")
        cmd, script = ran[0]
        self.assertEqual(cmd, ["osascript", "-", 'say "hi"', "Later", "Open"])
        self.assertNotIn('say "hi"', script)

    def test_a_cancelled_dialog_is_none_and_a_cancelled_secret_is_empty(self):
        def cancelled(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="",
                                               stderr="User canceled.")
        with mock.patch.object(dialogs, "can_show", return_value=True):
            self.assertIsNone(dialogs.alert("x", run=cancelled))
            self.assertEqual(dialogs.ask_secret("x", run=cancelled), "")

    def test_quit_with_a_key_typed_is_still_quit(self):
        """Only the Cancel button cancels an AppleScript dialog; Quit is
        an ordinary button, so the script itself has to hand back nothing
        for it - or the half-typed key went off to OpenAI to be checked
        and its refusal came up instead of the app quitting."""
        self.assertIn('if button returned of answer is "Quit" then '
                      'return ""', dialogs._SECRET)
        self.assertLess(dialogs._SECRET.index('is "Quit"'),
                        dialogs._SECRET.index("return text returned"))

    def test_off_macos_there_is_no_dialog(self):
        with mock.patch.object(dialogs.sys, "platform", "linux"):
            self.assertFalse(dialogs.can_show())
            self.assertIsNone(dialogs.alert("x", run=lambda *a, **k:
                                            self.fail("ran osascript")))


if __name__ == "__main__":
    unittest.main()
