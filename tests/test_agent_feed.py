"""The provider's own status feed, and what we are allowed to conclude.

`claude agents --json` is supported and scriptable ("does not require a
TTY"). It is a better answer to "is this worker actually working?" than
reading a transcript's mtime, because a worker parked on a prompt still has
a fresh transcript and reads as busy.

The rule these tests protect: uncertainty is never retirement. A feed that
times out, errors, or does not list a session must not turn a live worker
into a dead one.

Run with:  python3 -m unittest tests.test_agent_feed -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import agent_feed
from conductor.runtime import AGENT_STATUSES

ROWS = [
    {"pid": 1, "cwd": "/w/alpha", "sessionId": "s-a", "name": "alpha",
     "status": "idle"},
    {"pid": 2, "cwd": "/w/beta", "sessionId": "s-b", "name": "beta",
     "status": "busy", "state": "working"},
]


def fake_proc(stdout: str, code: int = 0):
    proc = mock.Mock()
    proc.returncode = code
    proc.communicate = mock.AsyncMock(return_value=(stdout.encode(), b""))
    return proc


class Reading(unittest.IsolatedAsyncioTestCase):
    async def test_it_parses_the_feed(self):
        with mock.patch("asyncio.create_subprocess_exec",
                        new=mock.AsyncMock(
                            return_value=fake_proc(json.dumps(ROWS)))):
            rows = await agent_feed.sessions()
        self.assertEqual(len(rows), 2)

    async def test_a_missing_binary_yields_nothing_not_an_error(self):
        with mock.patch("asyncio.create_subprocess_exec",
                        side_effect=OSError("no such binary")):
            self.assertEqual(await agent_feed.sessions(), [])

    async def test_a_nonzero_exit_yields_nothing(self):
        with mock.patch("asyncio.create_subprocess_exec",
                        new=mock.AsyncMock(
                            return_value=fake_proc("boom", code=1))):
            self.assertEqual(await agent_feed.sessions(), [])

    async def test_unparseable_output_yields_nothing(self):
        with mock.patch("asyncio.create_subprocess_exec",
                        new=mock.AsyncMock(
                            return_value=fake_proc("not json"))):
            self.assertEqual(await agent_feed.sessions(), [])

    async def test_a_timeout_yields_nothing(self):
        with mock.patch("asyncio.create_subprocess_exec",
                        new=mock.AsyncMock(return_value=fake_proc("[]"))), \
             mock.patch("asyncio.wait_for",
                        side_effect=asyncio.TimeoutError):
            self.assertEqual(await agent_feed.sessions(), [])

    async def test_the_stale_api_key_is_dropped(self):
        captured = {}

        async def spy(*args, **kw):
            captured.update(kw.get("env") or {})
            return fake_proc("[]")
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-stale"}), \
             mock.patch("asyncio.create_subprocess_exec", new=spy):
            await agent_feed.sessions()
        self.assertNotIn("ANTHROPIC_API_KEY", captured)


class Matching(unittest.TestCase):
    def test_by_pid_session_id_and_cwd(self):
        self.assertEqual(agent_feed.find(ROWS, pid=2)["name"], "beta")
        self.assertEqual(agent_feed.find(ROWS, session_id="s-a")["name"],
                         "alpha")
        self.assertEqual(agent_feed.find(ROWS, cwd="/w/beta")["name"], "beta")

    def test_no_match_is_none_not_a_guess(self):
        self.assertIsNone(agent_feed.find(ROWS, cwd="/w/nowhere"))
        self.assertIsNone(agent_feed.find([], pid=1))

    def test_the_private_var_symlink_still_matches(self):
        """macOS reports /private/var where callers pass /var; matching on
        the raw string silently fails to find a live worker."""
        with tempfile.TemporaryDirectory() as tmp:
            resolved = str(Path(tmp).resolve())
            rows = [{"pid": 9, "cwd": resolved, "status": "busy"}]
            self.assertIsNotNone(agent_feed.find(rows, cwd=tmp))

    def test_an_empty_handle_never_matches_everything(self):
        self.assertIsNone(agent_feed.find(ROWS, cwd=""))


class Concluding(unittest.TestCase):
    def test_state_outranks_status(self):
        self.assertEqual(agent_feed.status_of(ROWS[1]), "running")

    def test_idle_is_idle(self):
        self.assertEqual(agent_feed.status_of(ROWS[0]), "idle")

    def test_an_unknown_value_is_running_not_gone(self):
        self.assertEqual(
            agent_feed.status_of({"status": "something-new"}), "running")

    def test_no_row_is_disconnected(self):
        self.assertEqual(agent_feed.status_of(None), "disconnected")

    def test_every_answer_is_a_real_agent_status(self):
        for row in ROWS + [None, {}, {"status": "?"}]:
            self.assertIn(agent_feed.status_of(row), AGENT_STATUSES)


if __name__ == "__main__":
    unittest.main()


class TheRestartCase(unittest.IsolatedAsyncioTestCase):
    """The one place the feed beats the transcript, and it is measured.

    tmux panes outlive the app. After a restart the runtime holds no record
    of a worker that is sitting there alive, and reported it disconnected -
    so every existing worker looked dead until something re-adopted it.

    Where the runtime DOES have a record it is not consulted: against real
    workers (a quick task, a long one, and a command auto mode stops to ask
    about) the two sources agreed at every sample, so asking would cost a
    subprocess per status check to learn nothing.
    """

    def runtime(self):
        from conductor.tmux_runtime import TmuxClaudeRuntime
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        rt.sessions = {}
        rt.claude = "claude"
        return rt

    async def test_an_unknown_but_live_worker_is_not_called_dead(self):
        rows = [{"pid": 5, "cwd": "/w", "sessionId": "sess-live",
                 "status": "idle"}]
        with mock.patch("conductor.agent_feed.sessions",
                        new=mock.AsyncMock(return_value=rows)):
            self.assertEqual(await self.runtime().get_status("sess-live"),
                             "idle")

    async def test_an_unknown_worker_absent_from_the_feed_is_dead(self):
        with mock.patch("conductor.agent_feed.sessions",
                        new=mock.AsyncMock(return_value=[{"sessionId": "x"}])):
            self.assertEqual(await self.runtime().get_status("sess-gone"),
                             "disconnected")

    async def test_a_feed_that_fails_does_not_resurrect_anything(self):
        with mock.patch("conductor.agent_feed.sessions",
                        new=mock.AsyncMock(return_value=[])):
            self.assertEqual(await self.runtime().get_status("sess-any"),
                             "disconnected")

    async def test_a_known_session_never_asks_the_feed(self):
        rt = self.runtime()
        sess = mock.Mock(name="s", status="running", working_directory="/w")
        sess.name = "cond_x"
        rt.sessions["sess-known"] = sess
        rt._alive = lambda _n: True
        with mock.patch("conductor.agent_feed.sessions",
                        new=mock.AsyncMock()) as feed:
            self.assertEqual(await rt.get_status("sess-known"), "running")
        feed.assert_not_awaited()
