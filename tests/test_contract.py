"""Provider runtime contract suite.

Every CodingAgentRuntime implementation must pass the same behavioural
contract - when CodexRuntime arrives, it inherits this suite unchanged and
no Conductor test needs modification (spec section 24's architecture test).

Runs against FakeCodingAgentRuntime here; the live Claude runtime is covered
by tests/smoke_claude_runtime.py, which exercises the same operations against
the real provider.

Run with:  python3 -m unittest tests.test_contract -v
"""

from __future__ import annotations

import asyncio
import unittest

from conductor.agent_events import AgentEvent
from conductor.runtime import AGENT_STATUSES
from conductor.testing import FakeCodingAgentRuntime


class RuntimeContract:
    """Mix in with unittest.TestCase and provide make_runtime()."""

    def make_runtime(self):
        raise NotImplementedError

    def run_async(self, coro):
        return asyncio.run(coro)

    # -- session creation ------------------------------------------------
    def test_create_returns_durable_session_id(self):
        async def go():
            runtime = self.make_runtime()
            sid = await runtime.create_session("task_x", "/work/dir",
                                               "do the thing")
            self.assertTrue(sid)
            self.assertIn(await runtime.get_status(sid), AGENT_STATUSES)
            return sid
        self.run_async(go())

    def test_create_honors_directory_and_prompt(self):
        async def go():
            runtime = self.make_runtime()
            await runtime.create_session("task_x", "/work/dir", "the prompt")
            kind, task_id, cwd, prompt = runtime.calls[0]
            self.assertEqual(cwd, "/work/dir")
            self.assertEqual(prompt, "the prompt")
        self.run_async(go())

    # -- continuation -----------------------------------------------------
    def test_send_reaches_same_session(self):
        async def go():
            runtime = self.make_runtime()
            sid = await runtime.create_session("t", "/d", "p")
            await runtime.send(sid, "follow-up")
            self.assertIn(("send", sid, "follow-up"), runtime.calls)
        self.run_async(go())

    # -- events ---------------------------------------------------------------
    def test_events_reach_subscribers_and_unsubscribe_works(self):
        async def go():
            runtime = self.make_runtime()
            sid = await runtime.create_session("t", "/d", "p")
            seen = []
            unsubscribe = await runtime.subscribe(sid, seen.append)
            runtime.emit(sid, AgentEvent(type="progress", summary="working"))
            runtime.emit(sid, AgentEvent(type="completed", summary="done"))
            unsubscribe()
            runtime.emit(sid, AgentEvent(type="failed", error="late"))
            self.assertEqual([e.type for e in seen],
                             ["progress", "completed"])
        self.run_async(go())

    # -- interrupt / resume -------------------------------------------------
    def test_interrupt_then_resume(self):
        async def go():
            runtime = self.make_runtime()
            sid = await runtime.create_session("t", "/d", "p")
            await runtime.interrupt(sid)
            self.assertEqual(await runtime.get_status(sid), "idle")
            await runtime.resume(sid, working_directory="/d")
            self.assertIn(await runtime.get_status(sid), ("idle", "running"))
        self.run_async(go())

    # -- missing session / destroy ----------------------------------------
    def test_missing_session_is_controlled(self):
        async def go():
            runtime = self.make_runtime()
            self.assertEqual(await runtime.get_status("sess_nope"),
                             "disconnected")
            with self.assertRaises((KeyError, RuntimeError)):
                await runtime.send("sess_nope", "hello")
        self.run_async(go())

    def test_destroy_releases_session(self):
        async def go():
            runtime = self.make_runtime()
            sid = await runtime.create_session("t", "/d", "p")
            await runtime.destroy(sid)
            self.assertEqual(await runtime.get_status(sid), "disconnected")
            with self.assertRaises((KeyError, RuntimeError)):
                await runtime.send(sid, "after destroy")
        self.run_async(go())


class FakeRuntimeContractTest(RuntimeContract, unittest.TestCase):
    def make_runtime(self):
        return FakeCodingAgentRuntime()


if __name__ == "__main__":
    unittest.main()
