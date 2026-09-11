"""A Boss whose process has exited is not alive, whatever its window says.

Measured, 01:33-01:43: the Boss's claude was gone, cmux still showed its
workspace with the title claude had set ("✳ Coding session"), the runtime
answered alive for it, and every spoken message typed into an empty shell
and failed after 45 seconds with "never returned to its prompt". The
process is the Boss; the window is where it was.

Run with:  python3 -m unittest tests.test_boss_liveness -v
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import mock

try:
    from conductor.pty_manager import PtyManagerBackend
except Exception:          # pragma: no cover - needs the mcp package
    PtyManagerBackend = None


@unittest.skipIf(PtyManagerBackend is None, "pty_manager needs mcp")
class TheProcessIsTheBoss(unittest.TestCase):
    def backend(self, workspace_status="idle", process=True):
        from conductor.tmux_runtime import TmuxClaudeRuntime
        b = PtyManagerBackend.__new__(PtyManagerBackend)
        # A real PTY host, since only those keep a window after its
        # process has gone; its status probe is stubbed.
        b.runtime = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        b.runtime.get_status = mock.AsyncMock(return_value=workspace_status)
        b.boss_dir = Path("/tmp/boss")
        b._process_running = lambda: process
        b.recorded = []
        b.record = lambda type_, payload=None: b.recorded.append((type_, payload))
        return b

    def test_a_live_workspace_with_no_process_is_dead(self):
        b = self.backend(workspace_status="idle", process=False)
        self.assertFalse(asyncio.run(b._alive("sess")))
        self.assertTrue(b.recorded, "nothing was written down about it")
        self.assertIn("gone", b.recorded[0][1]["text"])

    def test_a_live_workspace_with_its_process_is_alive(self):
        b = self.backend(workspace_status="idle", process=True)
        self.assertTrue(asyncio.run(b._alive("sess")))
        self.assertEqual(b.recorded, [])

    def test_a_runtime_without_panes_is_not_asked_about_processes(self):
        """Only a PTY host can have a window outlive its process. A
        runtime with no panes (the SDK one, the test fakes) is believed
        as it is."""
        class NoPanes:
            async def get_status(self, session_id):
                return "idle"
        b = self.backend(process=False)
        b.runtime = NoPanes()
        self.assertTrue(asyncio.run(b._alive("sess")))

    def test_a_dead_workspace_is_dead_without_asking_the_process(self):
        b = self.backend(workspace_status="disconnected", process=True)
        self.assertFalse(asyncio.run(b._alive("sess")))

    def test_the_process_is_found_by_the_config_only_it_names(self):
        b = PtyManagerBackend.__new__(PtyManagerBackend)
        b.boss_dir = Path("/tmp/boss")
        with mock.patch("conductor.pty_manager.subprocess.run") as run:
            run.return_value = mock.Mock(stdout="4242\n", returncode=0)
            self.assertTrue(b._process_running())
            self.assertEqual(run.call_args.args[0][:2], ["pgrep", "-f"])
            self.assertIn("/tmp/boss/mcp.json", run.call_args.args[0][2])
            run.return_value = mock.Mock(stdout="", returncode=1)
            self.assertFalse(b._process_running())

    def test_not_being_able_to_ask_is_not_evidence_of_death(self):
        """A pgrep that fails must not retire a Boss that is fine."""
        b = PtyManagerBackend.__new__(PtyManagerBackend)
        b.boss_dir = Path("/tmp/boss")
        with mock.patch("conductor.pty_manager.subprocess.run",
                        side_effect=OSError("no pgrep")):
            self.assertTrue(b._process_running())


if __name__ == "__main__":
    unittest.main()

@unittest.skipIf(PtyManagerBackend is None, "pty_manager needs mcp")
class ALiveBossIsRead(unittest.TestCase):
    """Measured 2026-08-29, 15:16:58Z: the Boss's transcript watcher ended
    on one bad cmux listing; the Boss stayed alive and kept answering, and
    for six hours every turn timed out because nobody was reading. Every
    turn now asks the runtime to read again if it stopped."""

    def backend(self, rewatch):
        from conductor.tmux_runtime import TmuxClaudeRuntime
        b = PtyManagerBackend.__new__(PtyManagerBackend)
        b.runtime = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        b.runtime.get_status = mock.AsyncMock(return_value="idle")
        b.runtime.rewatch = rewatch
        b.boss_dir = Path("/tmp/boss")
        b._process_running = lambda: True
        b.session = object()                   # the record is already bound
        b.session_id = "boss-sid"
        b.recorded = []
        b.record = lambda type_, payload=None: b.recorded.append((type_, payload))
        return b

    def test_a_boss_whose_watcher_ended_is_watched_again(self):
        rewatch = mock.Mock(return_value=True)
        b = self.backend(rewatch)
        with mock.patch("conductor.pty_manager.application_log") as log:
            self.assertEqual(asyncio.run(b._ensure_session(None)), "boss-sid")
        rewatch.assert_called_once_with("boss-sid")
        self.assertIn("boss.watcher_restarted",
                      [c.args[1] for c in log.call_args_list])
        self.assertTrue(any("not being read" in (p or {}).get("text", "")
                            for _, p in b.recorded))

    def test_a_boss_still_being_read_is_left_alone(self):
        rewatch = mock.Mock(return_value=False)
        b = self.backend(rewatch)
        with mock.patch("conductor.pty_manager.application_log") as log:
            self.assertEqual(asyncio.run(b._ensure_session(None)), "boss-sid")
        rewatch.assert_called_once_with("boss-sid")
        self.assertEqual(log.call_args_list, [])
        self.assertEqual(b.recorded, [])

    def test_a_worker_update_also_finds_a_boss_nobody_is_reading(self):
        """Pushes do not go through _ensure_session; they are the other
        way to speak to a Boss whose watcher ended."""
        rewatch = mock.Mock(return_value=True)
        b = self.backend(rewatch)
        b.runtime.send = mock.AsyncMock()
        b._pending_updates, b._pushes_open = [], 0
        b._told_user_in_push, b._push_texts = False, []
        with mock.patch("conductor.pty_manager.application_log"):
            asyncio.run(b._push(["task_a: done"]))
        rewatch.assert_called_once_with("boss-sid")
        b.runtime.send.assert_awaited_once()

    def test_a_runtime_without_watchers_is_not_asked(self):
        class NoWatchers:
            async def get_status(self, session_id):
                return "idle"
        b = self.backend(rewatch=None)
        b.runtime = NoWatchers()
        self.assertEqual(asyncio.run(b._ensure_session(None)), "boss-sid")
        self.assertEqual(b.recorded, [])
