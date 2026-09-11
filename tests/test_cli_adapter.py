"""The runtime asks its adapter about the CLI it hosts, and nothing else.

Phase 1 of docs/any-cli.md: Claude Code's answers moved out of
tmux_runtime.py into ClaudeCodeAdapter with no change in behaviour (the
existing runtime suite is that proof), and the runtime consults the
adapter at every seam - which a recording adapter shows here.

Run with:  python3 -m unittest tests.test_cli_adapter -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from conductor.agent_events import AgentEvent
from conductor.cli_adapter import (ClaudeCodeAdapter, CliAdapter, adapter_for,
                                   normalize_entry)
from conductor import tmux_runtime
from conductor.tmux_runtime import TmuxClaudeRuntime, _TmuxSession


class RecordingAdapter(CliAdapter):
    name = "fake-cli"
    binary_name = "fakecli"

    def __init__(self):
        super().__init__("/bin/fakecli")
        self.asked: list[str] = []
        self.dir = Path(tempfile.mkdtemp())

    def launch_argv(self, prompt, permission_mode, session_id=None):
        self.asked.append("launch_argv")
        return [self.binary, "--go", prompt]

    def resume_argv(self, session_id, permission_mode=None):
        self.asked.append("resume_argv")
        return [self.binary, "--continue", session_id]

    def prompt_ready(self, screen):
        self.asked.append("prompt_ready")
        return "READY>" in screen

    def approval_prompt(self, screen):
        self.asked.append("approval_prompt")
        return "run it?" if "allow?" in screen else None

    def startup_dialog(self, screen):
        self.asked.append("startup_dialog")
        return None

    def transcript_dir(self, working_directory):
        self.asked.append("transcript_dir")
        return self.dir

    def normalize(self, entry, state):
        self.asked.append("normalize")
        if entry.get("done"):
            return [AgentEvent(type="completed", summary=entry.get("text", ""))]
        return [AgentEvent(type="progress", summary=entry.get("text", ""))]


class TheClaudeAdapterIsWhatTheRuntimeDid(unittest.TestCase):
    def test_launch_and_resume_lines(self):
        a = ClaudeCodeAdapter("/usr/local/bin/claude")
        self.assertEqual(a.launch_argv("fix login", "auto"),
                         ["/usr/local/bin/claude", "--permission-mode", "auto",
                          "--model=opus", "--effort=medium",
                          "--disallowedTools=AskUserQuestion",
                          "fix login"])
        self.assertEqual(a.launch_argv("fix login", "auto", session_id="sid"),
                         ["/usr/local/bin/claude", "--permission-mode", "auto",
                          "--model=opus", "--effort=medium",
                          "--disallowedTools=AskUserQuestion",
                          "--session-id", "sid", "fix login"])
        self.assertEqual(a.resume_argv("sid"),
                         ["/usr/local/bin/claude", "--resume", "sid",
                          "--model=opus", "--effort=medium",
                          "--disallowedTools=AskUserQuestion"])

    def test_workers_never_get_the_question_menu(self):
        """AskUserQuestion's option menu blocks the pane: the relayed voice
        answer moves the highlight but never submits, the turn never
        completes, and the Manager is told nothing is waiting. Both a
        fresh launch and a resume must deny it."""
        a = ClaudeCodeAdapter("/usr/local/bin/claude")
        for argv in (a.launch_argv("fix login", "auto"), a.resume_argv("sid")):
            flag = next(x for x in argv
                        if x.startswith("--disallowedTools="))
            denied = flag.split("=", 1)[1].split(",")
            self.assertIn("AskUserQuestion", denied)

    def test_the_deny_flag_never_touches_the_prompt(self):
        """--disallowedTools is variadic: given as a separate token, it
        swallows the positional prompt as another tool name and the worker
        launches idle with no goal. The flag must be a single =-joined
        token, and the prompt must stay the last argument."""
        a = ClaudeCodeAdapter("/usr/local/bin/claude")
        for argv in (a.launch_argv("fix login", "auto"),
                     a.launch_argv("fix login", "auto", session_id="sid")):
            self.assertNotIn("--disallowedTools", argv)
            self.assertEqual(argv[-1], "fix login")
        self.assertNotIn("--disallowedTools", a.resume_argv("sid"))

    def test_the_ready_prompt_the_dialogs_and_the_approval_shape(self):
        a = ClaudeCodeAdapter()
        self.assertTrue(a.prompt_ready("  ⏵⏵ bypass permissions on (shift+tab to cycle)"))
        self.assertFalse(a.prompt_ready("Last login: Fri\n$ "))
        self.assertEqual(a.startup_dialog("Do you trust this folder?"), "trust")
        self.assertEqual(a.startup_dialog("Resume from summary (recommended)"),
                         "resume_picker")
        self.assertEqual(a.startup_dialog(
            "WARNING: Claude Code running in Bypass Permissions mode\n"
            "❯ No, exit\n  Yes, I accept"), "bypass")
        self.assertIsNone(a.startup_dialog("❯ "))
        self.assertIsNone(a.startup_dialog(
            "⏵⏵ bypass permissions on (shift+tab to cycle)"))
        self.assertEqual(a.approval_prompt("Bash(rm -rf build)\nDo you want to proceed?\n❯ 1. Yes"),
                         "Bash(rm -rf build)")
        self.assertIsNone(a.approval_prompt("all quiet"))

    def test_the_transcript_lives_where_claude_writes_it(self):
        a = ClaudeCodeAdapter()
        d = a.transcript_dir("/tmp/some project")
        self.assertEqual(d.parent, tmux_runtime.CLAUDE_PROJECTS)
        self.assertEqual(d.name, tmux_runtime.munge_project_dir("/tmp/some project"))
        self.assertEqual(a.transcript_for("/tmp/x", "sid").name, "sid.jsonl")

    def test_normalisation_is_the_same_function(self):
        a = ClaudeCodeAdapter()
        entry = {"type": "assistant", "message": {"stop_reason": "end_turn",
                                                  "content": [{"type": "text", "text": "done"}]}}
        self.assertEqual([e.type for e in a.normalize(entry, {})],
                         [e.type for e in normalize_entry(entry, {})])

    def test_the_old_names_still_import_from_the_runtime(self):
        for name in ("CLAUDE_PROJECTS", "PROMPT_READY", "detect_approval_prompt",
                     "munge_project_dir", "normalize_entry"):
            self.assertTrue(hasattr(tmux_runtime, name), name)

    def test_adapter_for_knows_claude_and_falls_back_generically(self):
        self.assertIsInstance(adapter_for("claude-code"), ClaudeCodeAdapter)
        other = adapter_for("somecli")
        self.assertEqual(other.name, "somecli")
        self.assertIsNone(other.transcript_dir("/tmp"))
        self.assertTrue(other.prompt_ready("anything"))


@unittest.skipUnless(tmux_runtime.shutil.which("tmux"), "tmux not installed")
class TheRuntimeAsksItsAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = RecordingAdapter()
        self.rt = TmuxClaudeRuntime(transcript_dir=None, adapter=self.adapter)
        self.calls: list[list[str]] = []

        def fake_tmux(*args):
            self.calls.append(list(args))
            return tmux_runtime.subprocess.CompletedProcess(args, 0, "", "")
        self.rt._tmux = fake_tmux

    def test_the_default_adapter_is_claude_code(self):
        self.assertIsInstance(TmuxClaudeRuntime(transcript_dir=None).adapter,
                              ClaudeCodeAdapter)

    def test_launch_uses_the_adapters_command_line(self):
        async def go():
            # Adoption needs a transcript to appear; short-circuit at the
            # launch by making discovery fail fast.
            self.rt.startup_timeout = 0.01
            self.rt._alive = lambda name: True
            try:
                await self.rt.create_session("task_1", "/tmp", "do the thing")
            except RuntimeError:
                pass
        asyncio.run(go())
        launch = next(c for c in self.calls if c[0] == "new-session")
        self.assertEqual(launch[-3:], ["/bin/fakecli", "--go", "do the thing"])
        self.assertIn("launch_argv", self.adapter.asked)
        self.assertIn("transcript_dir", self.adapter.asked)

    def test_the_watcher_reads_through_the_adapters_normaliser(self):
        path = self.adapter.dir / "s1.jsonl"
        path.write_text(json.dumps({"text": "reading"}) + "\n"
                        + json.dumps({"text": "all done", "done": True}) + "\n")
        sess = _TmuxSession(task_id="task_1", name="cond_task_1",
                            working_directory="/tmp", session_id="s1",
                            jsonl_path=path)
        seen = []
        sess.handlers.append(seen.append)
        self.rt._alive = lambda name: True
        self.rt._pane = self._pane_ready

        async def go():
            return await self.rt._watch_once(sess)
        grew = asyncio.run(go())
        self.assertTrue(grew)
        self.assertEqual([(e.type, e.summary) for e in seen],
                         [("progress", "reading"), ("completed", "all done")])
        self.assertIn("normalize", self.adapter.asked)
        self.assertIn("approval_prompt", self.adapter.asked)
        self.assertEqual(sess.status, "idle")

    async def _pane_ready(self, name):
        return "READY>"

    def test_the_ready_prompt_is_the_adapters(self):
        sess = _TmuxSession(task_id="task_1", name="cond_task_1",
                            working_directory="/tmp", session_id="s1")
        self.rt._alive = lambda name: True
        self.rt._pane = self._pane_ready
        self.rt.startup_timeout = 2

        async def go():
            await self.rt._wait_resumed(sess)
        asyncio.run(go())
        self.assertTrue(sess.ready.is_set())
        self.assertIn("prompt_ready", self.adapter.asked)

    def test_a_transcriptless_cli_is_a_session_once_its_prompt_is_up(self):
        # The base adapter answers transcript_dir with None: there is no
        # file whose appearance proves the session started. Its prompt is
        # the proof, and discovery must adopt it - not time out and kill it.
        adapter = CliAdapter("/bin/anycli")
        rt = TmuxClaudeRuntime(transcript_dir=None, adapter=adapter)
        rt._tmux = lambda *args: tmux_runtime.subprocess.CompletedProcess(
            args, 0, "", "")
        rt._alive = lambda name: True
        rt._pane = self._pane_ready
        rt.startup_timeout = 2
        sess = _TmuxSession(task_id="task_1", name="cond_task_1",
                            working_directory="/tmp")

        async def go():
            await rt._discover_session_file(sess, set())
        asyncio.run(go())
        self.assertNotEqual(sess.status, "failed")
        self.assertTrue(sess.session_id.startswith("scr_"))
        self.assertIs(rt.sessions[sess.session_id], sess)

    def test_executions_name_the_adapters_provider(self):
        sess = _TmuxSession(task_id="task_1", name="cond_task_1",
                            working_directory="/tmp", session_id="s1")
        self.rt.sessions["s1"] = sess
        (execution,) = asyncio.run(self.rt.executions())
        self.assertEqual(execution.provider, "fake-cli")


if __name__ == "__main__":
    unittest.main()


def _assistant(text: str | None = None, tool: str | None = None,
               stop: str = "tool_use") -> dict:
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool is not None:
        content.append({"type": "tool_use", "name": tool,
                        "input": {"command": "true"}})
    return {"type": "assistant",
            "message": {"content": content, "stop_reason": stop}}


class AFinishIsTheWorkersLastMessage(unittest.TestCase):
    """Measured 2026-08-30 23:41:59Z (conductor-13163.jsonl, worker
    task_bd44c4cd): a turn was three lines of narration between tool
    calls and then a 980-character answer whose 470th character began
    "Fix: PR #110". The completed summary joined all four and cut the
    join at 600 from the front - so the result, the Boss's update and
    inspect_task all read "It's the same one-answer-spoken-twice race I
    found ... Nothing", and the user had to tell the Boss the PR
    existed."""

    NARRATION = ("It's the same one-answer-spoken-twice race I found — the "
                 "interim line and the final reply carry the same words. "
                 "Rather than explain it a third time, I'll fix it. Reading "
                 "the exact code paths.",
                 "Tests green (69). Now, one gated sequence: full suite → "
                 "commit → push → PR → back to my task branch.")
    ANSWER = ("Nothing further is needed — the fix is up.\n\n"
              "**Why you were hearing repeats:** a slow Boss turn speaks its "
              "first sentence early, then speaks the final answer with that "
              "sentence cut off the front. When the interim was the whole "
              "answer, the cut left nothing and the code fell back to the "
              "full answer — so the same paragraph was spoken twice, back to "
              "back (conductor-13163.jsonl, 23:34:44.508 → .512Z).\n\n"
              "**Fix: PR #110** — https://github.com/tamaratran/voice-agent/"
              "pull/110 — an empty remainder now means \"already said\". "
              "Five new tests; full suite 1352.\n\n"
              "The running conductor doesn't have this yet — it will keep "
              "repeating until #110 is merged and the conductor restarted.")

    def _turn(self) -> list[AgentEvent]:
        state: dict = {}
        events: list[AgentEvent] = []
        for text in self.NARRATION:
            events += normalize_entry(_assistant(text, tool="Bash"), state)
            events += normalize_entry(_assistant(tool="Bash"), state)
        events += normalize_entry(_assistant(self.ANSWER, stop="end_turn"),
                                  state)
        return events

    def test_the_completed_summary_is_the_final_message_whole(self):
        done = [e for e in self._turn() if e.type == "completed"]
        self.assertEqual(len(done), 1)
        summary = done[0].summary
        self.assertTrue(summary.startswith("Nothing further is needed"),
                        summary[:80])
        self.assertIn("Fix: PR #110", summary)
        self.assertTrue(summary.endswith("the conductor restarted."),
                        summary[-60:])
        self.assertNotIn("Reading the exact code paths", summary)
        self.assertNotIn("Tests green (69)", summary)
        # The narration is still reported, as it happens - as progress.
        self.assertIn("Tests green (69). Now, one gated sequence: full suite "
                      "→ commit → push → PR → back to my task branch.",
                      [e.summary for e in self._turn() if e.type == "progress"])

    def test_a_turn_that_ends_in_thought_still_reports_its_last_words(self):
        state: dict = {}
        events = normalize_entry(_assistant("Looking.", tool="Bash"), state)
        events += normalize_entry(_assistant("Opened PR #7.", stop="tool_use"),
                                  state)
        # A thinking-only entry closes the turn; the next message ends it.
        events += normalize_entry({"type": "assistant", "message": {
            "content": [{"type": "thinking", "thinking": "..."}],
            "stop_reason": "end_turn"}}, state)
        events += normalize_entry({"type": "user", "message": {
            "content": "next"}}, state)
        done = [e for e in events if e.type == "completed"]
        self.assertEqual([e.summary for e in done], ["Opened PR #7."])

    def test_a_subagents_turn_is_not_the_workers_finish(self):
        """Claude Code writes a Task-tool subagent's messages into the same
        session file, marked isSidechain. The subagent's final message
        carries end_turn - and read as the worker's, it completed the task
        mid-work: the Boss relayed a finish while the worker's own pane
        kept running."""
        state: dict = {}
        events = normalize_entry(_assistant("Delegating.", tool="Task"),
                                 state)
        side_user = {"type": "user", "isSidechain": True,
                     "message": {"content": "explore the codebase"}}
        side_done = dict(_assistant("Sidechain findings.", stop="end_turn"),
                         isSidechain=True)
        events += normalize_entry(side_user, state)
        events += normalize_entry(side_done, state)
        self.assertEqual([e.type for e in events
                          if e.type in ("completed", "progress")],
                         ["progress", "progress"])  # the worker's own only
        self.assertNotIn("completed", [e.type for e in events])
        # The worker's real finish still closes the turn, with its words.
        events = normalize_entry(_assistant("All done: PR #9.",
                                            stop="end_turn"), state)
        done = [e for e in events if e.type == "completed"]
        self.assertEqual([e.summary for e in done], ["All done: PR #9."])
        # And the sidechain's prose never pollutes the worker's summary.
        self.assertNotIn("Sidechain findings.",
                         [e.summary for e in events])

    def test_a_finish_too_long_to_carry_keeps_its_end(self):
        from conductor.agent_events import SUMMARY_CEILING, keep_end
        preamble = "Here is what I looked at. " * 400       # 10,400 chars
        text = preamble + "Outcome: opened PR #12."
        kept = keep_end(text, SUMMARY_CEILING)
        self.assertLessEqual(len(kept), SUMMARY_CEILING + 1)
        self.assertTrue(kept.startswith("…Here is what"), kept[:40])
        self.assertTrue(kept.endswith("Outcome: opened PR #12."))
        # Short enough: untouched. No sentence to start at: a word.
        self.assertEqual(keep_end("all done", 100), "all done")
        self.assertEqual(keep_end("abcdef ghij klmno", 9), "…klmno")
        # Nothing to cut at: the raw tail rather than nothing.
        self.assertEqual(keep_end("abcdefghijklmnop", 4), "…mnop")
        state: dict = {}
        _, done = normalize_entry(_assistant(text, stop="end_turn"), state)
        self.assertEqual(done.type, "completed")
        self.assertTrue(done.summary.endswith("Outcome: opened PR #12."))
