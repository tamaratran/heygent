"""A worker's scrollback comes from its transcript, not its pane.

Measured 2026-09-01 on a live worker, after the alternate screen was
turned off (#167): `alternate-screen off`, `alternate_on=0`, and still
`history_size=0`. A controlled pane showed why - a TUI that repaints in
place leaves history_size=0, while ordinary scrolling output leaves
106 - so `capture-pane -S -2000` returns exactly the visible screen and
there is nothing above it to scroll into.

The past is in the session's own transcript, which the runtime already
knows how to read.

Run with:  python3 -m unittest tests.test_worker_history -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import worker_history
from conductor.app_web import _history_rows

SESSION = "f7af59af-26cc-4a2b-a21f-e69c5a16b812"


def entry(kind, **fields):
    return json.dumps({"type": kind, **fields})


class ATranscriptIsTheScrollback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.task = "task_abc12345"
        folder = self.home / "projects" / "proj_1" / "tasks" / self.task
        folder.mkdir(parents=True)
        self.workspace = self.home / "workspaces" / "proj_1" / self.task
        self.workspace.mkdir(parents=True)
        (folder / "subagent.json").write_text(json.dumps({
            "task_id": self.task, "provider": "claude-code",
            "provider_session_id": SESSION,
            "workspace": {"path": str(self.workspace)}}))
        self.projects = Path(self.tmp.name) / "claude-projects"
        self.projects.mkdir()
        from conductor import tmux_runtime
        folder_name = tmux_runtime.munge_project_dir(str(self.workspace))
        self.transcript = self.projects / folder_name / f"{SESSION}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.patch = mock.patch.object(tmux_runtime, "CLAUDE_PROJECTS",
                                       self.projects)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write(self, *lines):
        self.transcript.write_text("\n".join(lines) + "\n")

    def a_turn(self, said="Found it.", tool="Bash(ls)"):
        return [
            entry("user", message={"role": "user", "content": "look at the tests"}),
            entry("assistant", message={"stop_reason": "tool_use", "content": [
                {"type": "text", "text": said},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}),
            entry("assistant", message={"stop_reason": "end_turn", "content": [
                {"type": "text", "text": "Done."}]}),
        ]

    def test_the_worker_s_own_words_come_back_in_order(self):
        self.write(*self.a_turn())
        rows = worker_history.lines(self.home, self.task)
        self.assertEqual([kind for kind, _ in rows],
                         ["you", "said", "tool", "said", "end"])
        self.assertEqual(rows[0][1], "look at the tests")
        self.assertIn("Found it.", rows[1][1])
        self.assertIn("Bash", rows[2][1])

    def test_a_worker_with_no_transcript_has_no_history_and_no_crash(self):
        self.assertEqual(worker_history.lines(self.home, self.task), [])
        self.assertEqual(worker_history.lines(self.home, "task_nothing"), [])

    def test_a_broken_line_does_not_lose_the_rest(self):
        self.write(entry("user", message={"role": "user", "content": "one"}),
                   "{not json",
                   entry("user", message={"role": "user", "content": "two"}))
        self.assertEqual([text for _, text in
                          worker_history.lines(self.home, self.task)],
                         ["one", "two"])

    def test_only_the_recent_turns_are_sent(self):
        """A worker that has run all day should not send its morning on
        every open."""
        self.write(*[line for _ in range(40) for line in self.a_turn()])
        rows = worker_history.lines(self.home, self.task, turns=5)
        self.assertEqual(len([1 for kind, _ in rows if kind == "end"]), 5)
        self.assertLess(len(rows), 40 * 5)

    def test_the_page_gets_rows_it_can_draw(self):
        self.write(*self.a_turn())
        rows = _history_rows(self.home, self.task)
        self.assertEqual(rows[0], {"kind": "you", "text": "look at the tests"})
        self.assertTrue(all(set(r) == {"kind", "text"} for r in rows))

    def test_the_page_still_opens_when_history_cannot_be_read(self):
        with mock.patch.object(worker_history, "lines",
                               side_effect=OSError("gone")):
            self.assertEqual(_history_rows(self.home, self.task), [])

    def test_a_codex_worker_is_read_with_codex_s_adapter(self):
        folder = self.home / "projects" / "proj_1" / "tasks" / "task_codex01"
        folder.mkdir(parents=True)
        (folder / "subagent.json").write_text(json.dumps({
            "provider": "codex", "provider_session_id": SESSION,
            "workspace": {"path": str(self.workspace)}}))
        path, provider = worker_history.transcript_of(self.home, "task_codex01")
        self.assertEqual(provider, "codex")
        # Codex files its rollouts elsewhere; the point is the adapter
        # was asked, not this machine's contents.
        self.assertTrue(path is None or "rollout" in str(path))


if __name__ == "__main__":
    unittest.main()
