"""Every CLI a worker runs in starts in its own bypass mode, and comes back in it.

Asked 2026-09-10: each CLI asks permission in its own words and keys, and
we cannot answer all of them reliably, so a worker runs with its CLI's
version of bypass permissions. And Devin and Droid are workers too. Their
working screens are not measured (neither is signed in here), so the
screens below are the log-in screens captured from 3000.6.7 and 0.73.0 in
a private tmux server, plus fakes built from the binaries' own strings.

Run with:  python3 -m unittest tests.test_bypass_every_cli -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import boss
from conductor.cli_adapter import adapter_for
from conductor.screen_adapter import DevinAdapter, DroidAdapter
from conductor.task_types import PROVIDERS


def contains(argv: list[str], flags: list[str]) -> bool:
    """flags appear in argv, in order and next to each other."""
    return any(argv[i:i + len(flags)] == flags
               for i in range(len(argv) - len(flags) + 1))


class TempSettings(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        patch = mock.patch.object(DroidAdapter, "SETTINGS_DIR", self.dir)
        patch.start()
        self.addCleanup(patch.stop)


class EveryBuiltInCliGetsItsBypassFlag(TempSettings):
    # What bypassPermissions must become on each command line.
    EXPECTED = {
        "claude-code": ["--permission-mode", "bypassPermissions"],
        "codex": ["--dangerously-bypass-approvals-and-sandbox"],
        "gemini": ["--approval-mode", "yolo"],
        "cursor": ["--force"],
        "devin": ["--permission-mode", "dangerous"],
    }

    def test_the_default_is_bypass(self):
        self.assertEqual(boss.WORKER_PERMISSION_MODE, "bypassPermissions")

    def test_on_launch(self):
        for provider, flags in self.EXPECTED.items():
            with self.subTest(provider):
                argv = adapter_for(provider, f"/bin/{provider}").launch_argv(
                    "the brief", boss.WORKER_PERMISSION_MODE)
                self.assertTrue(contains(argv, flags), argv)
                self.assertEqual(argv[-1], "the brief")

    def test_on_resume(self):
        """A CLI does not remember the mode it was launched in. Resumed
        without the flag, a worker came back in the CLI's own default and
        its first question waited in a pane nobody was watching."""
        for provider, flags in self.EXPECTED.items():
            with self.subTest(provider):
                argv = adapter_for(provider, f"/bin/{provider}").resume_argv(
                    "0b9a0c4e-1111-2222-3333-444455556666",
                    permission_mode=boss.WORKER_PERMISSION_MODE)
                self.assertTrue(contains(argv, flags), argv)

    def test_resume_without_a_mode_is_the_command_it_was(self):
        self.assertEqual(adapter_for("codex", "/bin/codex").resume_argv("sid"),
                         ["/bin/codex", "resume", "sid"])
        self.assertEqual(adapter_for("cursor", "/bin/c").resume_argv("chat_1"),
                         ["/bin/c", "--trust", "--resume", "chat_1"])

    def test_droid_gets_autonomy_high_from_a_settings_overlay(self):
        for argv in (
                adapter_for("droid", "/bin/droid").launch_argv(
                    "the brief", "bypassPermissions"),
                adapter_for("droid", "/bin/droid").resume_argv(
                    "scr_1", permission_mode="bypassPermissions")):
            with self.subTest(argv=argv):
                path = Path(argv[argv.index("--settings") + 1])
                self.assertEqual(json.loads(path.read_text()),
                                 {"sessionDefaultSettings":
                                  {"autonomyLevel": "high"}})

    def test_every_provider_is_in_the_table_a_person_reads(self):
        for provider in PROVIDERS:
            self.assertTrue(boss.PROVIDER_BYPASS_FLAGS.get(provider), provider)

    def test_a_configured_cli_gets_its_flags_on_resume_too(self):
        from conductor.configured_adapter import ConfiguredAdapter
        tool = ConfiguredAdapter("aider", {
            "command": ["aider"], "resume": ["aider", "--restore"],
            "permission_flags": {"bypassPermissions": ["--yes-always"]}})
        self.assertEqual(tool.resume_argv("scr_1", "bypassPermissions")[1:],
                         ["--restore", "--yes-always"])
        self.assertEqual(tool.resume_argv("scr_1")[1:], ["--restore"])


class TheRuntimeResumesInItsMode(unittest.TestCase):
    def resume_line(self, adapter):
        from conductor.tmux_runtime import TmuxClaudeRuntime
        runtime = TmuxClaudeRuntime(adapter=adapter)
        seen = []

        def fake_tmux(*args):
            seen.append(args)
            return mock.Mock(returncode=1, stdout="", stderr="stop here")
        with mock.patch.object(runtime, "_tmux", side_effect=fake_tmux), \
                mock.patch.object(runtime, "_alive", return_value=False), \
                mock.patch.object(runtime, "_live_worker_in", return_value=None):
            with self.assertRaises(RuntimeError):
                asyncio.run(runtime.resume(
                    "0b9a0c4e-1111-2222-3333-444455556666",
                    working_directory="/tmp/task_abc"))
        return next(a for a in seen if a and a[0] == "new-session")

    def test_claude_code(self):
        from conductor.cli_adapter import ClaudeCodeAdapter
        line = list(self.resume_line(ClaudeCodeAdapter("/bin/claude")))
        self.assertIn("--resume", line)
        self.assertTrue(contains(line, ["--permission-mode", "bypassPermissions"]),
                        line)

    def test_codex(self):
        from conductor.codex_adapter import CodexAdapter
        line = list(self.resume_line(CodexAdapter("/bin/codex")))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", line)


# Captured 2026-09-10 in a private tmux server, signed out.
DEVIN_LOGIN = """Welcome to Devin CLI!
How would you like to log in?

❭ 1 Log in with browser
    Recommended for most users
· 2 Paste a token manually
    For SSH or remote sessions without browser access
· 3 Log in with Windsurf for Enterprise
    For enterprise customers only
↑↓ select · ↵ confirm · esc cancel
"""

DROID_LOGIN = """                                                                             v0.73.0

                                                You are standing in an open terminal. An AI awaits your commands.

                                                  ENTER to send • \\ + ENTER for a new line • @ to mention files

╭──────────────────────────────────────────────────────────────────────╮
│ Welcome to Factory CLI                                               │
╰──────────────────────────────────────────────────────────────────────╯

Please login with your Factory account to continue.

> Login
  Exit
"""


class DevinIsAScreenAdapter(unittest.TestCase):
    def setUp(self):
        self.d = DevinAdapter("/bin/devin")

    def test_it_is_reachable_by_provider_name(self):
        self.assertIsInstance(adapter_for("devin"), DevinAdapter)
        from conductor.capabilities import provider_binaries, provider_label
        self.assertEqual(provider_binaries("devin"), ("devin",))
        self.assertEqual(provider_label("devin"), "Devin")

    def test_the_brief_comes_after_a_double_dash(self):
        """`devin [PATH]... [-- <PROMPT>...]`: a brief without the "--" is
        a path, and Devin Desktop opens on it instead."""
        argv = self.d.launch_argv("fix the login bug", "bypassPermissions")
        self.assertEqual(argv, ["/bin/devin", "--permission-mode", "dangerous",
                                "--", "fix the login bug"])
        self.assertEqual(self.d.launch_argv("x", "auto")[2], "smart")

    def test_resume_by_id_or_the_checkouts_latest(self):
        self.assertEqual(self.d.resume_argv("sess-1", "bypassPermissions"),
                         ["/bin/devin", "--permission-mode", "dangerous",
                          "--resume", "sess-1"])
        self.assertEqual(self.d.resume_argv("scr_ab12", "bypassPermissions"),
                         ["/bin/devin", "--permission-mode", "dangerous",
                          "--continue"])

    def test_the_log_in_screen_is_the_users_to_answer(self):
        self.assertEqual(self.d.startup_dialog(DEVIN_LOGIN), "auth")
        self.assertFalse(self.d.prompt_ready(DEVIN_LOGIN))
        self.assertIsNone(self.d.approval_prompt(DEVIN_LOGIN))

    def test_the_trust_dialog(self):
        screen = ("Do you trust the authors of /tmp/task_abc?\n"
                  "❭ Yes, trust this directory\n· No, exit\n")
        self.assertEqual(self.d.startup_dialog(screen), "trust")

    READY = ("  Devin\n\n > Ask Devin to build features, fix bugs, or work on "
             "your code\n  bypass permissions on\n")
    WORKING = ("  Reading src/app.py\n\n > Guide Devin while it works\n"
               "  bypass permissions on\n")

    def test_ready_is_the_empty_box_and_not_a_working_one(self):
        self.assertTrue(self.d.prompt_ready(self.READY))
        self.assertTrue(self.d.busy(self.WORKING))
        self.assertFalse(self.d.prompt_ready(
            self.WORKING + "\n> \n"), "a bare prompt under a busy hint")

    def test_a_turn_does_not_end_while_it_says_it_is_working(self):
        state: dict = {}
        screens = [self.READY, self.WORKING] + [self.WORKING + ">\n"] * 8
        events = [e.type for s in screens for e in self.d.screen_events(s, state)]
        self.assertNotIn("completed", events)
        done = "  The login bug was a stale token.\n" + self.READY
        events = [e.type for _ in range(8)
                  for e in self.d.screen_events(done, state)]
        self.assertEqual(events.count("completed"), 1)

    def test_ordinary_words_are_not_an_approval(self):
        self.assertIsNone(self.d.approval_prompt(
            "I'll allow the cache to expire; do you want me to approve it?\n"))
        self.assertIsNotNone(self.d.approval_prompt(
            "Run shell: rm -rf build\n❭ Yes, allow once\n· No, deny\n"))


class DroidIsAScreenAdapter(TempSettings):
    def setUp(self):
        super().setUp()
        self.d = DroidAdapter("/bin/droid")

    def test_it_is_reachable_by_provider_name(self):
        self.assertIsInstance(adapter_for("droid"), DroidAdapter)
        from conductor.capabilities import provider_label
        self.assertEqual(provider_label("droid"), "Droid")

    def test_the_overlay_is_settings_json_shaped(self):
        """From the 0.73 binary: the runtime file is merged like
        settings.json, and a "general" wrapper is refused outright."""
        argv = self.d.launch_argv("fix it", "bypassPermissions")
        self.assertEqual(argv[0], "/bin/droid")
        self.assertEqual(argv[1], "--settings")
        self.assertEqual(argv[-1], "fix it")
        body = json.loads(Path(argv[2]).read_text())
        self.assertNotIn("general", body)
        self.assertEqual(body["sessionDefaultSettings"]["autonomyLevel"], "high")
        # One file per level, rewritten only when it differs.
        self.assertEqual(self.d.launch_argv("x", "bypassPermissions")[2], argv[2])
        auto = self.d.launch_argv("x", "auto")
        self.assertIn("medium", Path(auto[2]).read_text())
        self.assertEqual(self.d.launch_argv("x", "default"), ["/bin/droid", "x"])

    def test_an_unwritable_overlay_starts_droid_plain(self):
        with mock.patch.object(DroidAdapter, "SETTINGS_DIR",
                               Path("/dev/null/nope")):
            self.assertEqual(self.d.launch_argv("x", "bypassPermissions"),
                             ["/bin/droid", "x"])

    def test_resume_never_guesses_a_session(self):
        """A bare --resume takes droid's last modified session anywhere."""
        argv = self.d.resume_argv("scr_ab12", "bypassPermissions")
        self.assertNotIn("--resume", argv)
        self.assertEqual(self.d.resume_argv("f00d", "bypassPermissions")[-2:],
                         ["--resume", "f00d"])

    def test_the_log_in_screen_is_the_users_to_answer(self):
        self.assertEqual(self.d.startup_dialog(DROID_LOGIN), "auth")
        self.assertFalse(self.d.prompt_ready(DROID_LOGIN))
        self.assertIsNone(self.d.approval_prompt(DROID_LOGIN))

    def test_busy_holds_the_turn_open(self):
        working = "│ >                                   │\n  ⠋ Thinking... (Press ESC to stop)\n"
        self.assertTrue(self.d.busy(working))
        self.assertFalse(self.d.prompt_ready(working))
        self.assertTrue(self.d.prompt_ready("╭────╮\n│ > │\n╰────╯\n"))


if __name__ == "__main__":
    unittest.main()
