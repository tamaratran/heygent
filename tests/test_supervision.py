"""Supervision acceptance tests (spec sections 41-46): the Manager always
knows whether each subagent is working, idle, waiting for approval/input,
paused, interrupted, recovering, or finished - and uncertainty never
duplicates a worker.

Run with:  python3 -m unittest tests.test_supervision -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.agent_events import AgentEvent
from conductor.global_conductor import GlobalConductor
from conductor.surfaces import FakeSurface
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


class SupervisionWorld(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = base / "code"
        (self.roots / "posely" / ".git").mkdir(parents=True)
        self.runtime = FakeCodingAgentRuntime()
        self.surface = FakeSurface()
        self.conductor = GlobalConductor(
            home=base / "home", runtime=self.runtime,
            search_roots=[self.roots], surface=self.surface,
            workspace_factory=lambda project: FakeWorkspaceManager())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def make_task(self, title: str = "Fix login"):
        registered = self.conductor.locator.register(self.roots / "posely")
        return self.run_async(self.conductor.create_task(
            title, "goal", project_id=registered.id))

    def status_of(self, task_id: str) -> str:
        return self.run_async(self.conductor.subagent_for(task_id)).status


class ApprovalAwarenessTest(SupervisionWorld):
    def test_41_approval_is_a_state_not_stuck(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit_approval(sid, "appr_1", "Install Stripe package")
        self.assertEqual(self.status_of(task.id), "waiting_for_approval")
        listing = self.run_async(self.conductor.list_subagents())
        entry = next(e for e in listing if e["task_id"] == task.id)
        self.assertEqual(entry["status"], "waiting_for_approval")
        self.assertEqual(entry["pending_approvals"][0]["approval_id"],
                         "appr_1")
        # Nothing was replaced while waiting.
        self.assertEqual(self.runtime._counter, 1)
        self.assertEqual(len(self.surface.created), 1)
        # Resolution returns the SAME subagent to work.
        self.run_async(self.conductor.approve_task_action(task.id, "appr_1"))
        after = self.run_async(self.conductor.subagent_for(task.id))
        self.assertEqual(after.provider_session_id, sid)
        self.assertEqual(after.status, "working")


class PauseAwarenessTest(SupervisionWorld):
    def test_42_pause_retains_session_and_resumes_it(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        self.run_async(self.conductor.pause_task(task.id))
        self.assertEqual(self.status_of(task.id), "paused")
        self.run_async(self.conductor.pause_task(task.id))   # idempotent
        # Session retained; no new execution.
        self.assertEqual(self.runtime._counter, 1)
        # Events while paused are recorded but never move the lifecycle.
        self.runtime.emit(sid, AgentEvent(type="completed", summary="late"))
        self.assertEqual(self.status_of(task.id), "paused")
        # Resume picks the SAME session back up.
        self.run_async(self.conductor.resume_task(task.id))
        self.assertEqual(self.status_of(task.id), "working")
        after = self.run_async(self.conductor.subagent_for(task.id))
        self.assertEqual(after.provider_session_id, sid)
        self.assertIn(("send", sid, "Please continue with the task."),
                      self.runtime.calls)
        self.assertEqual(self.runtime._counter, 1)

    def test_pause_and_interrupt_are_distinct_states(self) -> None:
        paused = self.make_task("Paused work")
        interrupted = self.make_task("Interrupted work")
        self.run_async(self.conductor.pause_task(paused.id))
        self.run_async(self.conductor.interrupt_task(interrupted.id))
        self.assertEqual(self.status_of(paused.id), "paused")
        self.assertEqual(self.status_of(interrupted.id),
                         "waiting_for_input")   # interrupt awaits the user


class SilenceTest(SupervisionWorld):
    def test_44_silence_reconciles_never_replaces(self) -> None:
        task = self.make_task()
        # A long quiet command: health says busy; nothing is spawned.
        report = self.run_async(self.conductor.recover_task(task.id))
        self.assertEqual(report["action"], "none")
        self.assertEqual(self.status_of(task.id), "working")
        self.assertEqual(self.runtime._counter, 1)
        self.assertEqual(len(self.surface.created), 1)

    def test_45_approval_waits_forever_without_replacement(self) -> None:
        task = self.make_task()
        self.runtime.emit_approval(task.provider_session_id, "appr_x",
                                   "risky thing")
        # However long it waits, reconciliation sees the approval state.
        for _ in range(5):
            report = self.run_async(self.conductor.recover_task(task.id))
            self.assertEqual(report["action"], "none")
            self.assertEqual(report["health"], "waiting_for_approval")
        self.assertEqual(self.runtime._counter, 1)

    def test_recovering_is_visible_while_in_flight(self) -> None:
        task = self.make_task()
        self.runtime.vanish(task.provider_session_id)
        gate = asyncio.Event()
        original = self.runtime.resume

        async def slow_resume(session_id, working_directory=None):
            await gate.wait()
            return await original(session_id, working_directory)
        self.runtime.resume = slow_resume

        async def scenario():
            recovery = asyncio.ensure_future(
                self.conductor.recover_task(task.id))
            await asyncio.sleep(0.02)
            mid = await self.conductor.subagent_for(task.id)
            gate.set()
            await recovery
            return mid.status
        self.assertEqual(self.run_async(scenario()), "recovering")
        # Recovered: alive again - idle until the next instruction arrives.
        self.assertIn(self.status_of(task.id), ("working", "idle"))


class LifecycleGuardTest(SupervisionWorld):
    def test_37_terminal_states_reject_late_events(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit(sid, AgentEvent(type="failed", error="boom"))
        self.assertEqual(self.status_of(task.id), "failed")
        # A late buffered event must not resurrect the lifecycle.
        self.runtime.emit(sid, AgentEvent(type="completed", summary="late"))
        self.runtime.emit(sid, AgentEvent(type="progress", summary="ghost"))
        self.assertEqual(self.status_of(task.id), "failed")
        subagent = self.run_async(self.conductor.subagent_for(task.id))
        self.assertFalse(subagent.result.success)   # result not overwritten


class ConcurrentStatesTest(SupervisionWorld):
    def test_46_five_states_reported_accurately_at_once(self) -> None:
        working = self.make_task("A working")
        self.runtime.emit(working.provider_session_id, AgentEvent(
            type="progress", summary="Running auth tests"))

        approval = self.make_task("B approval")
        self.runtime.emit_approval(approval.provider_session_id, "appr_b",
                                   "Install Stripe dependency")

        paused = self.make_task("C paused")
        self.run_async(self.conductor.pause_task(paused.id))

        # The cap is 3 running; pausing C freed a slot for D.
        interrupted = self.make_task("D interrupted")
        self.run_async(self.conductor.interrupt_task(interrupted.id))

        completed = self.make_task("E completed")
        self.runtime.emit(completed.provider_session_id, AgentEvent(
            type="completed", summary="All done, tests pass."))

        listing = self.run_async(self.conductor.list_subagents())
        by_title = {e["title"]: e for e in listing}
        self.assertEqual(by_title["A working"]["status"], "working")
        self.assertEqual(by_title["A working"]["activity"],
                         "Running auth tests")
        self.assertEqual(by_title["B approval"]["status"],
                         "waiting_for_approval")
        self.assertEqual(by_title["C paused"]["status"], "paused")
        self.assertEqual(by_title["D interrupted"]["status"],
                         "waiting_for_input")
        self.assertEqual(by_title["E completed"]["status"],
                         "waiting_for_input")
        self.assertIn("All done", by_title["E completed"]["result_summary"])
        # Attention first: the approval outranks everything in the ordering.
        self.assertEqual(listing[0]["title"], "B approval")
        # Five workers, five sessions, zero duplicates.
        self.assertEqual(self.runtime._counter, 5)


if __name__ == "__main__":
    unittest.main()
