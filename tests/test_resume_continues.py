"""resume_task said "ok" over a worker that was not working, twice.

2026-09-11, task_3ef16ed0 after a conductor restart, from its transcript
and conductor-16577.jsonl:

    00:31:49Z  resume_task -> `claude --resume 170e90a8...` in a new pane
    00:31:50   the transcript: "Continue from where you left off." (isMeta)
               and a synthetic "No response requested." - then nothing
    00:31:51   resume_task: ok; the card says Running
    00:32:57   the Boss notices, sends a follow-up; the worker takes it
    00:36:11   the worker ends its turn with its findings
               ...and the conductor logs not one event for it

Two defects. The resumed process sits at its prompt, because resuming a
process is not resuming the work. And a session this runtime had never
seen was resumed without its transcript, so its watcher had nothing to
read: the finish never reached the Boss, the card stayed on a command
from twenty minutes before, and the Boss paused the worker as stuck.

Run with:  python3 -m unittest tests.test_resume_continues -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import tmux_runtime
from conductor.cli_adapter import normalize_entry
from conductor.global_conductor import GlobalConductor
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager
from conductor.tmux_runtime import TmuxClaudeRuntime

CWD = "/Users/x/.voice-conductor/workspaces/proj_1/task_3ef16ed0"
SESSION = "170e90a8-ce7a-43ec-bbe0-6350cfc4df0e"
NAME = "cond_task_3ef16ed0"

# What `claude --resume` wrote on its own, as measured (trimmed).
FILLER = [
    {"type": "user", "isMeta": True, "message": {
        "role": "user",
        "content": [{"type": "text", "text": "Continue from where you left off."}]}},
    {"type": "assistant", "isApiErrorMessage": False, "message": {
        "model": "<synthetic>", "role": "assistant",
        "stop_reason": "stop_sequence",
        "content": [{"type": "text", "text": "No response requested."}]}},
]
FINISH = {"type": "assistant", "message": {
    "role": "assistant", "stop_reason": "end_turn",
    "content": [{"type": "text", "text": "Findings: most of it exists."}]}}

IDLE = ("⏺ Done.\n" + "─" * 40 + "\n❯\xa0\n" + "─" * 40 +
        "\n  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents\n")
BUSY = ("✻ Thinking… (12s · esc to interrupt)\n" + "─" * 40 + "\n❯\xa0\n" +
        "─" * 40 + "\n  ⏵⏵ auto mode on (shift+tab to cycle)\n")


class WhatResumeWritesIsNotATurn(unittest.TestCase):
    def test_the_filler_produces_nothing(self):
        state: dict = {}
        events = [e for entry in FILLER for e in normalize_entry(entry, state)]
        self.assertEqual(events, [],
                         "the Boss would hear the worker finished with "
                         "'No response requested.'")

    def test_a_synthetic_api_error_is_still_a_turn_end(self):
        entry = {"type": "assistant", "isApiErrorMessage": True, "message": {
            "model": "<synthetic>", "stop_reason": "stop_sequence",
            "content": [{"type": "text", "text": "API Error: overloaded"}]}}
        events = normalize_entry(entry, {})
        self.assertEqual([e.type for e in events], ["progress", "completed"])

    def test_the_users_own_continue_is_still_theirs(self):
        entry = {"type": "user", "message": {
            "content": "Continue from where you left off."}}
        self.assertEqual(len(normalize_entry(entry, {})), 1)


class ARuntime:
    def runtime(self, panes: dict[str, str], alive: set[str]):
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        rt.sessions = {}
        rt.claude = "/bin/claude"
        rt.startup_timeout = 0.5
        rt.transcript = None
        rt.approval_policy = mock.Mock()
        self.calls: list[tuple] = []

        def tmux(*args):
            self.calls.append(args)
            target = args[args.index("-t") + 1] if "-t" in args else ""
            if args[0] == "has-session":
                return mock.Mock(returncode=0 if target in alive else 1,
                                 stdout="", stderr="")
            if args[0] == "new-session":
                alive.add(args[args.index("-s") + 1])
            if args[0] == "capture-pane":
                return mock.Mock(returncode=0, stdout=panes.get(target, ""),
                                 stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        rt._tmux = tmux
        rt._process_alive = lambda sess: False
        rt._watch = lambda sess: asyncio.sleep(0)
        return rt


class AResumedWorkerIsRead(ARuntime, unittest.TestCase):
    def test_its_turn_after_the_resume_reaches_the_conductor(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            folder = projects / tmux_runtime.munge_project_dir(CWD)
            folder.mkdir(parents=True)
            transcript = folder / f"{SESSION}.jsonl"
            transcript.write_text('{"type": "summary"}\n' * 5)
            history = transcript.stat().st_size
            rt = self.runtime({NAME: IDLE}, alive=set())
            with mock.patch.object(tmux_runtime, "CLAUDE_PROJECTS", projects):
                asyncio.run(rt.resume(SESSION, working_directory=CWD))
            self.assertTrue(any(c[0] == "new-session" for c in self.calls))
            sess = rt.sessions[SESSION]
            self.assertEqual(sess.jsonl_path, transcript,
                             "resumed with no transcript: nobody reads it")
            self.assertEqual(sess.offset, history,
                             "history would be replayed as new work")

            seen = []
            sess.handlers.append(seen.append)
            with transcript.open("a") as out:
                for entry in [*FILLER, FINISH]:
                    out.write(json.dumps(entry) + "\n")
            asyncio.run(rt._watch_once(sess))
            self.assertEqual([e.type for e in seen], ["progress", "completed"])
            self.assertEqual(seen[-1].summary, "Findings: most of it exists.")


class AtItsPrompt(ARuntime, unittest.TestCase):
    def ask(self, pane: str, alive=True, pending=None):
        rt = self.runtime({NAME: pane}, alive={NAME} if alive else set())
        sess = tmux_runtime._TmuxSession(task_id="task_3ef16ed0", name=NAME,
                                         working_directory=CWD,
                                         session_id=SESSION)
        sess.pending_approval = pending
        rt.sessions[SESSION] = sess
        return asyncio.run(rt.at_prompt(SESSION))

    def test_an_idle_prompt_is_idle(self):
        self.assertIs(self.ask(IDLE), True)

    def test_a_turn_in_progress_is_not(self):
        self.assertIs(self.ask(BUSY), False)

    def test_a_pending_approval_is_not(self):
        self.assertIs(self.ask(IDLE, pending={"approval_id": "appr_1"}), False)

    def test_a_permission_prompt_on_screen_is_not(self):
        pane = "Bash(rm -rf build)\nDo you want to proceed?\n❯ 1. Yes\n  2. No\n"
        self.assertIs(self.ask(pane), False)

    def test_a_gone_pane_cannot_say(self):
        self.assertIsNone(self.ask(IDLE, alive=False))
        rt = self.runtime({}, alive=set())
        self.assertIsNone(asyncio.run(rt.at_prompt("unknown")))


class PromptAware(FakeCodingAgentRuntime):
    """The fake runtime, plus the one question resume_task asks."""
    idle: bool | None = True

    async def at_prompt(self, session_id):
        return self.idle


class ResumeTaskContinuesTheWork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        roots = base / "code"
        (roots / "posely" / ".git").mkdir(parents=True)
        self.runtime = PromptAware()
        self.gc = GlobalConductor(
            home=base / "home", runtime=self.runtime, search_roots=[roots],
            workspace_factory=lambda project: FakeWorkspaceManager())
        project = self.gc.locator.register(roots / "posely")
        self.task = asyncio.run(self.gc.create_task(
            "Support any CLI agent", "goal", project_id=project.id))
        self.sid = self.task.provider_session_id

    def tearDown(self):
        self.tmp.cleanup()

    def continues(self) -> list:
        return [c for c in self.runtime.calls
                if c == ("send", self.sid, "Please continue with the task.")]

    def interrupted(self):
        asyncio.run(self.gc.interrupt_task(self.task.id))
        self.runtime.calls.clear()

    def test_a_worker_at_its_prompt_is_told_to_go_on(self):
        self.interrupted()
        asyncio.run(self.gc.resume_task(self.task.id))
        self.assertEqual(len(self.continues()), 1)

    def test_a_worker_already_at_work_is_left_alone(self):
        self.interrupted()
        self.runtime.idle = False
        asyncio.run(self.gc.resume_task(self.task.id))
        self.assertEqual(self.continues(), [])

    def test_running_by_the_runtimes_account_is_checked_on_the_pane(self):
        """get_status said running; the pane says idle. That was the
        no-op branch, and it answered ok over an idle worker."""
        self.runtime.statuses[self.sid] = "running"
        self.runtime.calls.clear()
        asyncio.run(self.gc.resume_task(self.task.id))
        self.assertEqual(len(self.continues()), 1)

    def test_a_runtime_that_cannot_tell_is_not_guessed_at(self):
        self.interrupted()
        self.runtime.idle = None
        asyncio.run(self.gc.resume_task(self.task.id))
        self.assertEqual(self.continues(), [])


if __name__ == "__main__":
    unittest.main()
