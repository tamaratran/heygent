"""A CLI with no transcript is followed off its screen.

Phase 2 of docs/any-cli.md: ScreenAdapter turns polls of a terminal
into the same AgentEvents a transcript produces - progress while the
screen moves, completed once the prompt is back and the screen has
settled - and GeminiAdapter is that plus Gemini's dialogs and flags.

Run with:  python3 -m unittest tests.test_screen_adapter -v
"""

from __future__ import annotations

import unittest

from conductor.cli_adapter import adapter_for
from conductor.screen_adapter import GeminiAdapter, ScreenAdapter


def poll(adapter, state, *screens):
    out = []
    for screen in screens:
        out += [(e.type, e.summary) for e in adapter.screen_events(screen, state)]
    return out


IDLE = "╭──────╮\n│ > \n╰──────╯\n"
WORKING = "Reading README.txt\n"
ANSWER = "Reading README.txt\nThe first line is: hello\n│ > \n"


class TheScreenIsTheTranscript(unittest.TestCase):
    def setUp(self):
        self.a = ScreenAdapter("/bin/somecli")
        self.a.SETTLE_POLLS = 2

    def test_movement_is_progress_and_a_settled_prompt_is_the_turn_end(self):
        state = {}
        events = poll(self.a, state, IDLE, IDLE, WORKING, ANSWER, ANSWER, ANSWER)
        self.assertEqual(events[0], ("progress", "Reading README.txt"))
        self.assertEqual(events[1], ("progress", "The first line is: hello"))
        self.assertEqual(events[-1][0], "completed")
        self.assertIn("The first line is: hello", events[-1][1])
        self.assertNotIn(">", events[-1][1], "the prompt is chrome, not text")
        self.assertFalse(state["screen_busy"])

    def test_a_prompt_that_flickers_between_tool_calls_does_not_end_the_turn(self):
        state = {}
        events = poll(self.a, state, IDLE, WORKING, ANSWER, WORKING + "more\n",
                      WORKING + "more\n│ > \n", WORKING + "more\n│ > \n",
                      WORKING + "more\n│ > \n")
        self.assertEqual([t for t, _ in events].count("completed"), 1)
        self.assertIn("more", events[-1][1])

    def test_nothing_happens_on_an_idle_screen(self):
        self.assertEqual(poll(self.a, {}, IDLE, IDLE, IDLE, IDLE), [])

    def test_no_transcript_on_disk(self):
        self.assertIsNone(self.a.transcript_dir("/tmp/x"))
        self.assertIsNone(self.a.transcript_for("/tmp/x", "s"))


class GeminiIsAScreenAdapterWithItsOwnDialogs(unittest.TestCase):
    def setUp(self):
        self.g = GeminiAdapter("/opt/homebrew/bin/gemini")

    def test_launch_maps_our_permission_mode_onto_geminis(self):
        self.assertEqual(self.g.launch_argv("fix login", "auto"),
                         ["/opt/homebrew/bin/gemini", "--approval-mode",
                          "auto_edit", "--prompt-interactive", "fix login"])
        self.assertEqual(self.g.launch_argv("x", "bypassPermissions")[2], "yolo")
        self.assertEqual(self.g.resume_argv("whatever"),
                         ["/opt/homebrew/bin/gemini", "--resume", "latest"])

    def test_the_dialogs_measured_on_0_35_2(self):
        trust = ("│ ● 1. Trust folder (gem)\n│   2. Trust parent folder (tmp)\n"
                 "│   3. Don't trust\n")
        self.assertEqual(self.g.startup_dialog(trust), "trust")
        # And the runtime can answer it: the framed, ●-marked option is
        # the one already selected, so no Downs, just Enter. Measured
        # live: a Gemini worker sat on this dialog because the frame
        # hid the option from choose_option.
        from conductor.tmux_runtime import choose_option
        self.assertEqual(choose_option(trust, "trust folder"), 0)
        self.assertEqual(choose_option(trust, "don't trust"), 2)
        auth = "│ ? Get started\n│ How would you like to authenticate for this project?\n"
        self.assertEqual(self.g.startup_dialog(auth), "auth")
        self.assertIsNone(self.g.startup_dialog("│ > \n"))

    def test_an_approval_names_what_is_asked(self):
        screen = ("│ Shell rm -rf build\n│ Allow execution?\n"
                  "│ ● 1. Yes, allow once\n│   2. Yes, allow always\n│   3. No\n")
        self.assertEqual(self.g.approval_prompt(screen), "Shell rm -rf build")
        self.assertIsNone(self.g.approval_prompt("│ > \n"))

    def test_it_is_reachable_by_provider_name(self):
        self.assertIsInstance(adapter_for("gemini"), GeminiAdapter)
        self.assertEqual(adapter_for("gemini").display, "Gemini CLI")


class CursorIsAScreenAdapterUntilASignedInTurnIsMeasured(unittest.TestCase):
    """Phase 4. Measured on cursor-agent 2026.08.25 (installed here on
    2026-08-30): the flags, and the login screen a signed-out CLI shows.
    A signed-in turn's screen is still to be measured; the prompt and
    approval markers are the generic shapes until then."""

    def test_launch_and_resume_lines_as_measured(self):
        from conductor.screen_adapter import CursorAdapter
        c = CursorAdapter("/usr/local/bin/cursor-agent")
        self.assertEqual(c.launch_argv("fix login", "auto"),
                         ["/usr/local/bin/cursor-agent", "--trust", "fix login"])
        self.assertEqual(c.launch_argv("fix login", "bypassPermissions"),
                         ["/usr/local/bin/cursor-agent", "--trust", "--force",
                          "fix login"])
        self.assertEqual(c.resume_argv("chat_1"),
                         ["/usr/local/bin/cursor-agent", "--trust", "--resume",
                          "chat_1"])
        self.assertEqual(adapter_for("cursor").name, "cursor")

    def test_the_login_screen_is_a_dialog_only_the_user_can_answer(self):
        from conductor.screen_adapter import CursorAdapter
        c = CursorAdapter("/usr/local/bin/cursor-agent")
        screen = ("                           Cursor Agent\n"
                  "                           v2026.08.25-3e8eec8\n"
                  "                           Press any key to log in...\n")
        self.assertEqual(c.startup_dialog(screen), "auth")
        self.assertFalse(c.prompt_ready(screen))

    READY = ("  Cursor Agent\n  v2026.08.25-3e8eec8\n"
             "  Tip: Use /debug to instrument and debug complex problems.\n"
             "  → Plan, search, build anything\n  Auto\n  ~/gem\n")
    ASKING = ("  Running the command and reporting the result.\n"
              "  $ touch /tmp/marker && ls -la /tmp/marker Waiting\n"
              "    for approval...\n"
              "────────────\n"
              " $  touch /tmp/marker && ls -la /tmp/marker in .\n"
              " Run this command?\n Not in allowlist: touch\n"
              "  → Run (once) (y)\n    Add Shell(touch) to allowlist? (tab)\n"
              "    Run Everything (shift+tab)\n"
              "    Skip & tell the agent what to do instead (esc or n)\n")

    def test_the_signed_in_screens_measured_on_2026_08_30(self):
        from conductor.screen_adapter import CursorAdapter
        c = CursorAdapter("/usr/local/bin/cursor-agent")
        self.assertTrue(c.prompt_ready(self.READY))
        self.assertIsNone(c.approval_prompt(self.READY))
        self.assertEqual(c.approval_prompt(self.ASKING),
                         "touch /tmp/marker && ls -la /tmp/marker in .")
        self.assertFalse(c.prompt_ready(self.ASKING),
                         "the arrow marks the selected option, not a ready box")
        # After an answer the composer's placeholder changes; the arrow
        # is still the prompt, and the turn is over once it holds still.
        after = ("  No errors were printed. The marker file is present.\n"
                 "  → Add a follow-up\n  Auto · 8.3%\n  ~/gem\n")
        self.assertTrue(c.prompt_ready(after))
        c.SETTLE_POLLS = 2
        state = {}
        events = poll(c, state, self.READY, "  Running the command.\n",
                      after, after, after)
        self.assertEqual(events[-1][0], "completed")
        self.assertIn("No errors were printed", events[-1][1])
        self.assertNotIn("Add a follow-up", events[-1][1])


import asyncio
import shutil

from conductor.tmux_runtime import TmuxClaudeRuntime, _TmuxSession


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TheRuntimeFollowsAScreenOnlyCli(unittest.TestCase):
    def setUp(self):
        self.adapter = ScreenAdapter("/bin/somecli")
        self.adapter.SETTLE_POLLS = 2
        self.rt = TmuxClaudeRuntime(transcript_dir=None, adapter=self.adapter)
        self.rt._tmux = lambda *args: __import__("subprocess").CompletedProcess(args, 0, "", "")
        self.rt._alive = lambda name: True
        self.screens: list[str] = []

        async def pane(name):
            return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        self.rt._pane = pane

    def test_the_watcher_reads_turns_off_the_screen(self):
        sess = _TmuxSession(task_id="task_1", name="cond_task_1",
                            working_directory="/tmp", session_id="scr_1")
        seen = []
        sess.handlers.append(seen.append)
        self.screens = [IDLE, WORKING, ANSWER, ANSWER, ANSWER, ANSWER]

        async def go():
            for _ in range(6):
                await self.rt._watch_once(sess)
        asyncio.run(go())
        self.assertEqual([e.type for e in seen][-1], "completed")
        self.assertIn("The first line is: hello", seen[-1].summary)
        self.assertEqual(sess.status, "idle")
        self.assertIsNone(sess.jsonl_path)

    def test_a_sign_in_screen_becomes_a_question_for_the_user_once(self):
        """Measured: a Gemini worker sat on "Sign in with Google" showing
        Working for four minutes. Nothing can be typed for the user; the
        card should say what is being asked of them."""
        self.rt.adapter = GeminiAdapter("/opt/homebrew/bin/gemini")
        sess = _TmuxSession(task_id="task_1", name="cond_task_1",
                            working_directory="/tmp", session_id="scr_1")
        seen = []
        sess.handlers.append(seen.append)
        auth = ("│ ? Get started\n│ How would you like to authenticate for this "
                "project?\n│ ● 1. Sign in with Google\n│   2. Use Gemini API Key\n")
        self.screens = [auth]

        async def go():
            # On the watcher, where the task is subscribed - not during
            # discovery, where it reached no one (measured).
            await self.rt._watch_once(sess)
            await self.rt._watch_once(sess)
        asyncio.run(go())
        self.assertEqual([(e.type, e.question) for e in seen],
                         [("needs_input", "Gemini CLI needs you to sign in - open its window")])

    def test_a_session_with_no_transcript_is_named_once_its_prompt_is_up(self):
        sess = _TmuxSession(task_id="task_1", name="cond_task_1",
                            working_directory="/tmp")
        self.screens = [IDLE]
        self.rt.startup_timeout = 2

        async def go():
            await self.rt._discover_session_file(sess, set())
        asyncio.run(go())
        self.assertTrue(sess.session_id.startswith("scr_"))
        self.assertIn(sess.session_id, self.rt.sessions)
        self.assertTrue(sess.ready.is_set())


if __name__ == "__main__":
    unittest.main()
