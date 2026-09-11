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

    def test_what_was_typed_is_the_user_line_and_not_the_answer(self):
        """No transcript writes the user line back, so the runtime that
        typed it says so - the Boss matches its utterances (and the
        worker updates it pushes) to that line, by its words. And the
        CLI drawing the words back, however wrapped, is not the reply."""
        state = {}
        poll(self.a, state, IDLE, IDLE)
        events = self.a.sent("What is the first   line of README?", state)
        self.assertEqual([(e.type, e.summary, e.detail) for e in events],
                         [("progress", "> What is the first line of README?",
                           {"source": "user_message"})])
        echoed = ("> What is the first line of\n  README?\n"
                  "Reading README.txt\nThe first line is: hello\n│ > \n")
        events = poll(self.a, state, echoed, echoed, echoed)
        self.assertEqual(events[0], ("progress", "The first line is: hello"))
        self.assertEqual(events[-1][0], "completed")
        self.assertEqual(events[-1][1], "Reading README.txt The first line is: hello")
        # A short line that happens to be a word of the message is not
        # a piece of it.
        self.assertFalse(self.a.is_echo("line", state))
        self.assertTrue(self.a.is_echo("│ › what is the first line of readme?", state))
        # Only the last few messages are remembered.
        for n in range(6):
            self.a.sent(f"message number {n}", state)
        self.assertEqual(len(state["screen_sent"]), ScreenAdapter.SENT_KEPT)

    def test_a_transcript_cli_reports_no_user_line_of_its_own(self):
        from conductor.cli_adapter import ClaudeCodeAdapter
        self.assertEqual(ClaudeCodeAdapter("/bin/claude").sent("hi", {}), [])


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

    def test_a_dialog_answered_and_scrolled_above_the_prompt_is_history(self):
        """Measured 0.59.0 (task_947be3d1): Gemini answers the trust
        choice by restarting in place, and its dialog stays on the pane
        above the new banner and prompt. The watcher read it as a live
        dialog for two minutes, never reached the worker's turn end and
        pressed Enter at the prompt on every poll."""
        screen = ("Do you trust the files in this folder?\n"
                  "● 1. Trust folder (task_947be3d1)\n"
                  "  2. Trust parent folder (proj_1fae0753)\n"
                  "  3. Don't trust\n\n"
                  "Gemini CLI is restarting to apply the trust changes...\n"
                  "   Gemini CLI v0.59.0\n"
                  "   Authenticated with gemini-api-key /auth\n\n"
                  "> Read README and report the first installation command.\n\n"
                  " ✓ ReadFile README.md\n"
                  "✦ The first installation command in the README is:\n\n"
                  "  1 curl -fsSL https://example/install.sh | bash\n\n"
                  "YOLO Ctrl+Y\n"
                  "* █ Type your message or @path/to/file\n"
                  "workspace (/directory)\n")
        self.assertIsNone(self.g.startup_dialog(screen))
        self.assertTrue(self.g.prompt_ready(screen))
        state = {"screen_last": ["Gemini CLI v0.59.0"], "screen_busy": True,
                 "screen_since": ["Gemini CLI v0.59.0"]}
        self.g.screen_events(screen, state)
        for _ in range(self.g.SETTLE_POLLS):
            events = self.g.screen_events(screen, state)
        self.assertEqual([e.type for e in events], ["completed"])
        self.assertIn("curl -fsSL", events[0].summary)
        # Still the dialog while nothing has been drawn under it - the
        # prompt above it (the previous session's) does not clear it.
        live = ("> earlier question\n\n* █ Type your message or @path/to/file\n"
                "Do you trust the files in this folder?\n"
                "● 1. Trust folder (task_947be3d1)\n  3. Don't trust\n")
        self.assertEqual(self.g.startup_dialog(live), "trust")

    def test_the_input_box_is_the_starred_one_and_the_echo_above_is_not(self):
        """Measured 0.59.0 in tmux, 2026-09-11: the box is " * text"
        between a ▄▄▄ and a ▀▀▀ rule; a submitted message is drawn back
        above as " > text" between the same rules, wrapped and indented.
        Reading ">" alone found the echo as the box, so a pushed update
        "stayed in the input box" for ever: Enter again, then typed
        again (boss.update_push_failed x3, one finish, three pushes)."""
        message = ("Your worker · Read README heading with Codex (task_63367cd9) "
                   "finished a turn: The first heading of README.md is voice-agent "
                   "and the tagline says hold a key talk and Claude Code answers "
                   "out loud. Please just acknowledge this in one short sentence "
                   "and do nothing else.")
        rule_top, rule_bottom = "▄" * 140, "▀" * 140
        cut = message.rindex(" ", 0, 130)
        first, rest = message[:cut], message[cut + 1:]
        typed = (f"{rule_top}\n * {first}\n   {rest}\n{rule_bottom}\n"
                 " workspace (/directory)   branch   sandbox   /model\n"
                 " ~/repos/voice-agent   devin/x   no sandbox   Auto\n")
        self.assertEqual(self.g.input_box(typed), message)
        self.assertEqual(self.g.draft(typed), message)
        submitted = (f"{rule_top}\n > {first}\n   {rest}\n{rule_bottom}\n"
                     " ⠇ Thinking... (esc to cancel, 0s)        ? for shortcuts\n"
                     "─" * 140 + "\n YOLO Ctrl+Y      3 skills\n"
                     f"{rule_top}\n *   Type your message or @path/to/file\n"
                     f"{rule_bottom}\n"
                     " workspace (/directory)   branch   sandbox   /model\n"
                     " ~/repos/voice-agent   devin/x   no sandbox   Auto\n")
        self.assertEqual(self.g.input_box(submitted),
                         "Type your message or @path/to/file")
        self.assertEqual(self.g.draft(submitted), "")
        self.assertTrue(self.g.busy(submitted))
        tail = "".join(message.split())[-40:]
        self.assertNotIn(tail, "".join(self.g.input_box(submitted).split()))
        # And the echo is not the reply: the rules are furniture.
        state: dict = {}
        self.g.sent(message, state)
        said = self.g.said(self.g.lines(submitted), state)
        self.assertFalse(any("Read README heading" in ln for ln in said), said)
        self.assertFalse(any(ln.startswith("▄") for ln in self.g.lines(submitted)))

    def test_a_turns_words_are_the_starred_paragraph_not_the_tool_results(self):
        """Measured 2026-09-11 (Gemini Boss, conductor-47755, 08:30:56):
        Gemini prints a tool call as a "✓ name (server) {args}" card
        with the whole result under it - for inspect_task, the task's
        JSON - and the Boss's reply went out as that JSON followed by
        its one "✦" sentence."""
        turn = ("✓ inspect_task (boss MCP Server) {\"task_id\":\"task_4bc29b78\"}\n"
                "  {\n    \"provider_health\": \"idle\",\n"
                "    \"result\": {\n      \"summary\": \"The README's first heading "
                "is **voice-agent**.\",\n      \"success\": true\n    }\n  }\n\n"
                "✓ complete_task (boss MCP Server) {\"task_id\":\"task_4bc29b78\"}\n"
                "  ok\n\n"
                "✦ I have completed the task. The Codex worker reported that the\n"
                "  first heading of the README is voice-agent.\n\n"
                "                                              ? for shortcuts\n"
                "YOLO Ctrl+Y                     1 GEMINI.md file · 1 MCP server\n"
                "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n *   Type your message or @path/to/file\n"
                "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
                "workspace (/directory)          sandbox          /model\n"
                "~/.voice-conductor/boss         no sandbox       Auto\n")
        state = {"screen_last": ["Gemini CLI v0.59.0"], "screen_busy": True,
                 "screen_since": ["Gemini CLI v0.59.0"]}
        self.g.screen_events(turn, state)
        for _ in range(self.g.SETTLE_POLLS):
            events = self.g.screen_events(turn, state)
        self.assertEqual([e.type for e in events], ["completed"])
        self.assertEqual(events[0].summary,
                         "I have completed the task. The Codex worker reported "
                         "that the first heading of the README is voice-agent.")
        # A screen with no "✦" (an older Gemini, a dialog) reads as before.
        self.assertEqual(self.g.said(["Some plain line", "another"], {}),
                         ["Some plain line", "another"])

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

    def test_a_sent_message_is_reported_as_the_user_line(self):
        sess = _TmuxSession(task_id="task_1", name="cond_task_1",
                            working_directory="/tmp", session_id="scr_1")
        self.rt.sessions["scr_1"] = sess
        seen = []
        sess.handlers.append(seen.append)
        self.screens = [IDLE]

        async def no_draft(sess):
            return ""

        async def moved(sess, before, submit):
            return None
        self.rt._wait_for_input = no_draft
        self.rt._confirm_moved = moved
        asyncio.run(self.rt.send("scr_1", "list the open tasks"))
        self.assertEqual([(e.type, e.summary, e.detail) for e in seen],
                         [("progress", "> list the open tasks",
                           {"source": "user_message"})])
        self.assertEqual(sess.state["screen_sent"], ["list the open tasks"])
        self.assertEqual(sess.status, "running")

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
