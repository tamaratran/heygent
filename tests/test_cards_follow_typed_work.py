"""A card has to move when the work moves, whoever asked for the work.

The transcript records every turn regardless of who caused it, so a
message the user TYPES into a worker's own window finishes exactly like a
spoken one. What it does not produce is a card, because cards are built
from what the watcher sees, and the watcher is a task per session that
only exists for sessions this process started. After a restart - or for a
worker adopted from an earlier run - nobody is reading it: the user
types, the agent answers, and the card still says "Working".

Run with:  python3 -m unittest tests.test_cards_follow_typed_work -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from conductor.tmux_runtime import TmuxClaudeRuntime, _TmuxSession

CWD = "/Users/x/.voice-conductor/workspaces/proj_1/task_abc"
SESSION = "sess-1"


class Base(unittest.IsolatedAsyncioTestCase):
    def runtime(self, alive=True):
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        rt.sessions = {}
        rt.transcript = None
        self.watched = []
        rt._tmux = lambda *a: mock.Mock(
            returncode=0 if alive else 1, stdout="", stderr="")
        # The process table agrees with the host; a test about a window
        # with nothing in it says so itself.
        rt._process_alive = lambda sess: alive
        rt._watch = lambda sess: self._record(sess)
        return rt

    async def _record(self, sess):
        self.watched.append(sess.name)
        await asyncio.sleep(0)


class WatchingWhatIsAlreadyRunning(Base):
    async def test_a_live_worker_nobody_reads_gets_a_watcher(self):
        rt = self.runtime()
        self.assertTrue(rt.ensure_watched(SESSION, CWD))
        await asyncio.sleep(0)
        self.assertEqual(self.watched, ["cond_task_abc"])
        self.assertIn(SESSION, rt.sessions)

    async def test_a_worker_already_watched_is_left_alone(self):
        """Restarting a healthy watcher would re-read the transcript and
        re-announce work that already finished."""
        rt = self.runtime()
        rt.ensure_watched(SESSION, CWD)
        await asyncio.sleep(0)
        self.assertFalse(rt.ensure_watched(SESSION, CWD))
        await asyncio.sleep(0)
        self.assertEqual(len(self.watched), 1)

    async def test_a_watcher_that_died_is_replaced(self):
        rt = self.runtime()
        sess = _TmuxSession(task_id="task_abc", name="cond_task_abc",
                            working_directory=CWD, session_id=SESSION)
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        sess.watcher = done
        rt.sessions[SESSION] = sess
        self.assertTrue(rt.ensure_watched(SESSION, CWD))
        await asyncio.sleep(0)
        self.assertEqual(self.watched, ["cond_task_abc"])

    async def test_nothing_running_is_not_watched(self):
        """There is no PTY to read, and inventing a session for it would
        make the card claim work that is not happening."""
        rt = self.runtime(alive=False)
        self.assertFalse(rt.ensure_watched(SESSION, CWD))
        self.assertEqual(self.watched, [])

    async def test_an_empty_window_is_not_watched(self):
        """The workspace is listed but the claude in it has exited.
        Watching it would read nothing for ever and keep a card saying
        "Working" over a bare shell."""
        rt = self.runtime()
        rt._process_alive = lambda sess: False
        self.assertFalse(rt.ensure_watched(SESSION, CWD))
        self.assertEqual(self.watched, [])
        self.assertNotIn(SESSION, rt.sessions)

    async def test_history_is_not_replayed_when_adopting(self):
        """The turns it already finished are history; re-reading them
        would announce old work as if it had just landed."""
        import tempfile
        from pathlib import Path

        from conductor import tmux_runtime
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            folder = projects / tmux_runtime.munge_project_dir(CWD)
            folder.mkdir(parents=True)
            transcript = folder / f"{SESSION}.jsonl"
            transcript.write_text('{"turn": 1}\n{"turn": 2}\n')
            rt = self.runtime()
            with mock.patch.object(tmux_runtime, "CLAUDE_PROJECTS", projects):
                rt.ensure_watched(SESSION, CWD)
            self.assertEqual(rt.sessions[SESSION].offset,
                             transcript.stat().st_size)


def _line(kind: str, text: str, stop: str | None = None) -> str:
    import json
    entry: dict = {"type": kind, "message": {"content": [
        {"type": "text", "text": text}]}}
    if stop:
        entry["message"]["stop_reason"] = stop
    return json.dumps(entry) + "\n"


class AFinishNobodyReadIsDelivered(Base):
    """Adoption starts at the END of the transcript, so a turn that
    finished in the unwatched window produced no event: the answer sat in
    the worker's pane, the Boss was never told, and the task said
    "running" for ever. The finish is recovered at adoption and delivered
    once a subscriber is attached."""

    def _adopt(self, transcript_text: str):
        import tempfile
        from pathlib import Path

        from conductor import tmux_runtime
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        projects = Path(self.tmp.name)
        folder = projects / tmux_runtime.munge_project_dir(CWD)
        folder.mkdir(parents=True)
        (folder / f"{SESSION}.jsonl").write_text(transcript_text)
        rt = self.runtime()
        with mock.patch.object(tmux_runtime, "CLAUDE_PROJECTS", projects):
            rt.ensure_watched(SESSION, CWD)
        return rt

    async def test_an_already_finished_turn_is_recovered(self):
        rt = self._adopt(_line("user", "do the thing")
                         + _line("assistant", "working on it")
                         + _line("assistant", "All done: PR #7.",
                                 stop="end_turn"))
        seen = []
        await rt.subscribe(SESSION, seen.append)
        self.assertTrue(rt.deliver_adopted_finish(SESSION))
        self.assertEqual([(e.type, e.summary) for e in seen],
                         [("completed", "All done: PR #7.")])
        self.assertEqual(rt.sessions[SESSION].status, "idle")
        # Delivered means delivered once.
        self.assertFalse(rt.deliver_adopted_finish(SESSION))
        self.assertEqual(len(seen), 1)

    async def test_an_open_turn_is_not_a_finish(self):
        """The worker is still going; the watcher will see it end."""
        rt = self._adopt(_line("user", "do the thing")
                         + _line("assistant", "working on it"))
        seen = []
        await rt.subscribe(SESSION, seen.append)
        self.assertFalse(rt.deliver_adopted_finish(SESSION))
        self.assertEqual(seen, [])

    async def test_a_subagents_end_is_not_recovered_as_a_finish(self):
        import json
        side = json.dumps({"type": "assistant", "isSidechain": True,
                           "message": {"content": [
                               {"type": "text", "text": "side findings"}],
                               "stop_reason": "end_turn"}}) + "\n"
        rt = self._adopt(_line("user", "do the thing")
                         + _line("assistant", "working on it") + side)
        self.assertFalse(rt.deliver_adopted_finish(SESSION))

    def test_the_sweep_delivers_after_subscribing(self):
        import inspect

        from conductor.global_conductor import GlobalConductor
        adopt = inspect.getsource(GlobalConductor.watch_unwatched)
        self.assertIn("_subscribe", adopt,
                      "an adopted watcher's events reach no one")
        self.assertLess(adopt.index("_subscribe"),
                        adopt.index("deliver_adopted_finish"),
                        "a finish delivered before subscribing is lost")


class TheSweepDoesIt(unittest.TestCase):
    def test_the_sweep_watches_running_tasks(self):
        import inspect

        from conductor.global_conductor import GlobalConductor
        source = ""
        for _, member in inspect.getmembers(GlobalConductor):
            if inspect.isfunction(member):
                text = inspect.getsource(member)
                if "unwatched_prompts" in text:
                    source = text
                    break
        self.assertIn("watch_unwatched", source,
                      "the sweep does not re-attach watchers to live workers")
        adopt = inspect.getsource(GlobalConductor.watch_unwatched)
        self.assertIn("ensure_watched", adopt,
                      "nothing re-attaches a watcher to a live worker")
        self.assertIn("task.watch_resumed", adopt,
                      "re-attaching is invisible in the log")
        # And at startup, not only thirty seconds later.
        self.assertIn("watch_unwatched",
                      inspect.getsource(GlobalConductor.startup))


if __name__ == "__main__":
    unittest.main()
