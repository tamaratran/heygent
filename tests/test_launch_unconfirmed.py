"""A launch the host could not confirm is judged by the session, not
presumed dead.

Measured 2026-08-29, three launches in a row: cmux reported "the command
never landed whole", claude was running in every pane, the task was
marked failed, its worktree was deleted under the worker, and the Boss
started a fourth worker on the same job (PRs #72, #73, #74). The session
either wrote a transcript or it did not; that decides, and a session
that did not start is closed rather than left behind.

Run with:  python3 -m unittest tests.test_launch_unconfirmed -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from conductor.cmux_runtime import _Result
from conductor.tmux_runtime import TmuxClaudeRuntime


class Base(unittest.TestCase):
    def runtime(self, alive: bool, pane: str = ""):
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        rt.sessions = {}
        self.tmux = []

        def _tmux(*args):
            self.tmux.append(args)
            if args[0] == "new-session":
                return _Result(1, "", "the command never landed whole in "
                                      "'cond_task_a''s shell")
            return _Result(0, "")
        rt._tmux = _tmux
        rt._alive = lambda name: alive
        rt._pane = mock.AsyncMock(return_value=pane)
        rt.adopt_session = mock.AsyncMock(return_value="sid-discovered")
        rt._adopt_known = mock.AsyncMock(return_value="sid-known")
        return rt


class AnUnconfirmedLaunch(Base):
    def test_a_session_that_is_not_there_is_the_failure_it_was(self):
        rt = self.runtime(alive=False)
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(rt.launch_session("task_a", "/tmp", ["claude"]))
        self.assertIn("never landed whole", str(caught.exception))
        rt.adopt_session.assert_not_called()

    def test_a_session_that_is_there_is_judged_by_its_transcript(self):
        """adopt_session waits for the transcript and closes the session
        itself when none appears - the same judgement a confirmed launch
        gets."""
        rt = self.runtime(alive=True)
        with mock.patch("conductor.tmux_runtime.application_log"):
            sid = asyncio.run(rt.launch_session("task_a", "/tmp", ["claude"]))
        self.assertEqual(sid, "sid-discovered")
        rt.adopt_session.assert_awaited_once()

    def test_a_known_id_session_must_reach_its_prompt(self):
        rt = self.runtime(alive=True, pane="Last login: ...\n$ ")
        with mock.patch("conductor.tmux_runtime.application_log"), \
                self.assertRaises(RuntimeError) as caught:
            asyncio.run(rt.launch_session("boss", "/tmp", ["claude"],
                                          session_id="known-id"))
        self.assertIn("no claude prompt appeared", str(caught.exception))
        self.assertEqual(self.tmux[-1], ("kill-session", "-t", "cond_boss"))

    def test_a_known_id_session_at_its_prompt_is_adopted(self):
        rt = self.runtime(alive=True,
                          pane="❯ \n  ⏵⏵ auto mode on (shift+tab to cycle)")
        with mock.patch("conductor.tmux_runtime.application_log"):
            sid = asyncio.run(rt.launch_session("boss", "/tmp", ["claude"],
                                                session_id="known-id"))
        self.assertEqual(sid, "sid-known")
        # The one kill is the pre-launch sweep of a same-named session;
        # after new-session the launch turns the alternate screen off
        # and pipes the raw stream for the window's real terminal.
        self.assertEqual(self.tmux[-1][0], "pipe-pane")
        self.assertEqual(self.tmux[-2][0], "set-option")
        self.assertIn("alternate-screen", self.tmux[-2])
        self.assertEqual(self.tmux[-3][0], "new-session")


if __name__ == "__main__":
    unittest.main()
