"""Phase 5 tests: the Conductor's manager tools, driven with a fake runtime.

No LLM anywhere - the point is that the seven tools are deterministic and
testable on their own, exactly as the spec orders the phases.

Run with:  python3 -m unittest tests.test_lifecycle -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor import task_events
from conductor.agent_events import AgentEvent
from conductor.conductor import Conductor
from conductor.testing import FakeCodingAgentRuntime as FakeRuntime


class ConductorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = FakeRuntime()
        self.conductor = Conductor(self.tmp.name, self.runtime)
        self.store = self.conductor.store

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_create_task_pipeline(self) -> None:
        task = self.run_async(self.conductor.create_task(
            "Fix login redirect", "Fix intermittent redirect after login"))
        self.assertEqual(task.status, "running")
        self.assertEqual(task.provider_session_id, "sess_1")
        self.assertIsNotNone(task.workspace)
        # The worker got the goal in its first prompt.
        kind, task_id, cwd, prompt = self.runtime.calls[0]
        self.assertEqual((kind, task_id), ("create", task.id))
        self.assertIn("Fix intermittent redirect", prompt)
        # context.md and events.jsonl exist from birth.
        self.assertTrue(self.store.context_path(task.id).exists())
        types = [e["type"] for e in task_events.read(self.store, task.id)]
        self.assertEqual(types, ["task_created", "agent_started"])

    def test_events_drive_status(self) -> None:
        task = self.run_async(self.conductor.create_task("t", "g"))
        sid = task.provider_session_id
        self.runtime.emit(sid, AgentEvent(type="completed",
                                          summary="Found the race condition."))
        self.assertEqual(self.store.get(task.id).status, "waiting_for_user")
        self.assertIn("Found the race condition.",
                      self.store.context_path(task.id).read_text())
        self.runtime.emit(sid, AgentEvent(type="failed", error="boom"))
        self.assertEqual(self.store.get(task.id).status, "failed")

    def test_a_worker_written_off_by_mistake_is_revived_when_used_again(self) -> None:
        """One bad host listing said "tmux session ended" for every
        session at once; the workers went on running and the Boss went on
        sending them work, and every report they made after that was
        dropped as terminal - 85 events, two finishes among them."""
        # The store on the conductor's bus, as the product wires it: that
        # is how a lifecycle decision reaches canonical subagent state.
        from conductor.observability import ObservabilityBus
        from conductor.task_store import TaskStore
        bus = ObservabilityBus()
        conductor = Conductor(self.tmp.name, self.runtime,
                              store=TaskStore(self.tmp.name, bus=bus), bus=bus)
        self.conductor, self.store = conductor, conductor.store
        task = self.run_async(self.conductor.create_task("t", "g"))
        sid = task.provider_session_id
        self.runtime.emit(sid, AgentEvent(type="failed", error="tmux session ended"))
        self.assertEqual(self.store.get(task.id).status, "failed")
        self.assertEqual(self.conductor.subagents.get(task.id).status, "failed")
        # The Boss talks to it again - the send reaches the session, so
        # the worker is there - and its next report counts.
        self.run_async(self.conductor.send_to_task(task.id, "and the tests?"))
        self.assertEqual(self.conductor.subagents.get(task.id).status, "working")
        self.runtime.emit(sid, AgentEvent(type="completed",
                                          summary="Tests pass on the branch."))
        self.assertEqual(self.store.get(task.id).status, "waiting_for_user")
        # A turn end, not a task end (the reducer's rule): standing by,
        # with the answer on the record.
        state = self.conductor.subagents.get(task.id)
        self.assertEqual(state.status, "idle")
        self.assertIn("Tests pass", (state.result or {}).get("summary", ""))

    def test_a_retired_task_answered_by_the_boss_reports_again(self) -> None:
        """The idle-retire sweep closed task_987f66d8 as completed
        (2026-08-31 21:00:24Z); the Boss sent it a follow-up thirteen
        minutes later, the worker answered on screen, and the turn end
        was dropped as `terminal` - no Worker update ever reached the
        Boss. send_to_task revives the task; the sidecar must follow."""
        from conductor.observability import ObservabilityBus
        from conductor.task_store import TaskStore
        bus = ObservabilityBus()
        conductor = Conductor(self.tmp.name, self.runtime,
                              store=TaskStore(self.tmp.name, bus=bus), bus=bus)
        self.conductor, self.store = conductor, conductor.store
        seen: list[str] = []
        bus.subscribe(lambda e: seen.append(e.type))
        task = self.run_async(self.conductor.create_task("t", "g"))
        sid = task.provider_session_id
        self.runtime.emit(sid, AgentEvent(type="completed",
                                          summary="First answer."))
        # Unaddressed long enough: the sweep closes it the way the user
        # would - complete_task, because it has a result.
        self.run_async(self.conductor.complete_task(task.id))
        self.assertEqual(self.conductor.subagents.get(task.id).status,
                         "completed")
        # The Boss follows up. The send reaches the session, so the
        # worker is there - and its next report counts.
        self.run_async(self.conductor.send_to_task(task.id, "and now?"))
        self.assertEqual(self.conductor.subagents.get(task.id).status,
                         "working")
        del seen[:]
        self.runtime.emit(sid, AgentEvent(type="completed",
                                          summary="Second answer."))
        state = self.conductor.subagents.get(task.id)
        self.assertEqual(state.status, "idle")
        self.assertIn("Second answer", (state.result or {}).get("summary", ""))
        self.assertEqual(self.store.get(task.id).status, "waiting_for_user")
        # The event that carries a finish to the Boss fired for this
        # turn - it is what becomes the "Worker update" line.
        self.assertIn("task.completed", seen)

    def test_a_stuck_pair_from_a_past_run_heals_when_the_worker_speaks(self) -> None:
        """The running-edge revival fires only in the process that sees
        the edge. task_987f66d8 was retired and revived under one
        conductor; the restart started the next with Task=running and
        sidecar=completed already in place, the status never changed
        again, and 13+ events - two turn ends among them - dropped as
        `terminal` (2026-08-31 22:15-22:19Z). An event from a live
        worker on an active task must heal the pair, not hit it."""
        from conductor.observability import ObservabilityBus
        from conductor.subagent_state import apply_lifecycle
        from conductor.task_store import TaskStore
        bus = ObservabilityBus()
        conductor = Conductor(self.tmp.name, self.runtime,
                              store=TaskStore(self.tmp.name, bus=bus), bus=bus)
        self.conductor, self.store = conductor, conductor.store
        seen: list[str] = []
        bus.subscribe(lambda e: seen.append(e.type))
        task = self.run_async(self.conductor.create_task("t", "g"))
        sid = task.provider_session_id
        # The pair as a restart finds it: the Task says running, the
        # sidecar - written by an earlier, pre-revival run - says done.
        stuck = apply_lifecycle(
            self.conductor.subagents.get(task.id)
            or self.conductor._subagent_state(self.store.get(task.id)),
            "completed")
        self.conductor.subagents.save(stuck)
        del seen[:]
        self.runtime.emit(sid, AgentEvent(type="completed",
                                          summary="The answer."))
        state = self.conductor.subagents.get(task.id)
        self.assertEqual(state.status, "idle")
        self.assertIn("The answer", (state.result or {}).get("summary", ""))
        self.assertEqual(self.store.get(task.id).status, "waiting_for_user")
        self.assertIn("task.completed", seen)
        self.assertNotIn("agent.event_dropped", seen)

    def test_send_reaches_session_and_context(self) -> None:
        task = self.run_async(self.conductor.create_task("t", "g"))
        self.run_async(self.conductor.send_to_task(
            task.id, "Do not modify OAuth"))
        self.assertIn(("send", task.provider_session_id,
                       "Do not modify OAuth"), self.runtime.calls)
        self.assertIn("Do not modify OAuth",
                      self.store.context_path(task.id).read_text())
        types = [e["type"] for e in task_events.read(self.store, task.id)]
        self.assertIn("user_instruction", types)

    def test_interrupt_resume_cancel(self) -> None:
        task = self.run_async(self.conductor.create_task("t", "g"))
        sid = task.provider_session_id

        self.run_async(self.conductor.interrupt_task(task.id))
        self.assertIn(("interrupt", sid), self.runtime.calls)
        self.assertEqual(self.store.get(task.id).status, "waiting_for_user")

        self.run_async(self.conductor.resume_task(task.id))
        self.assertEqual(self.store.get(task.id).status, "running")

        self.run_async(self.conductor.cancel_task(task.id))
        self.assertIn(("destroy", sid), self.runtime.calls)
        self.assertEqual(self.store.get(task.id).status, "cancelled")
        # A cancelled task's events stop flowing into state.
        self.runtime.emit(sid, AgentEvent(type="completed", summary="late"))
        self.assertEqual(self.store.get(task.id).status, "cancelled")

    def test_inspect(self) -> None:
        task = self.run_async(self.conductor.create_task("Fix login", "g"))
        report = self.conductor.inspect_task(task.id)
        self.assertEqual(report["task"]["id"], task.id)
        self.assertIn("# Fix login", report["context"])
        self.assertTrue(report["recent_events"])

    def test_unknown_task_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.conductor.inspect_task("task_missing")


class TaskEndingTest(unittest.TestCase):
    """When does a task complete? Only when the user says so.

    A PTY worker does not exit when it has answered - it ends a turn and
    waits, because the next thing the user says may be a follow-up. So
    the runtime's `completed` event is a turn end, and the terminal
    `completed` status had no producer at all: the only ways a task ever
    ended were cancel and a crash. Every finished task kept its window,
    its session and its worktree, and 44 worktrees piled up.
    """

    def setUp(self) -> None:
        from conductor.observability import ObservabilityBus
        from conductor.testing import FakeWorkspaceManager
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = FakeRuntime()
        self.workspaces = FakeWorkspaceManager()
        self.bus = ObservabilityBus()
        self.events = []
        self.bus.subscribe(self.events.append)
        self.conductor = Conductor(self.tmp.name, self.runtime,
                                   workspaces=self.workspaces, bus=self.bus)
        self.store = self.conductor.store

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def emitted(self, kind: str) -> list:
        return [e for e in self.events if e.type == kind]

    def test_the_workers_completed_event_does_not_complete_the_task(self):
        """A turn end is not the end of the task. The worker is waiting
        for the next instruction, and its directory stays with it."""
        task = self.run_async(self.conductor.create_task("t", "g"))
        self.runtime.emit(task.provider_session_id,
                          AgentEvent(type="completed", summary="done"))
        self.assertEqual(self.store.get(task.id).status, "waiting_for_user")
        self.assertIn(task.id, self.workspaces.workspaces)
        self.assertFalse(self.emitted("workspace.released"))

    def test_complete_keeps_the_session_and_the_workspace(self):
        """Measured 2026-09-01: the Boss completes a task seconds after
        its worker answers, and completion killed the pane - every card
        opened afterwards was a dead terminal. Finished work stays
        readable; close_pane is the teardown, later."""
        task = self.run_async(self.conductor.create_task("t", "g"))
        sid = task.provider_session_id
        self.run_async(self.conductor.complete_task(task.id))
        self.assertEqual(self.store.get(task.id).status, "completed")
        self.assertNotIn(("destroy", sid), self.runtime.calls)
        self.assertIn(task.id, self.workspaces.workspaces)
        self.assertEqual(self.emitted("workspace.released"), [])

    def test_close_pane_ends_the_session_and_releases_the_workspace(self):
        task = self.run_async(self.conductor.create_task("t", "g"))
        sid = task.provider_session_id
        self.run_async(self.conductor.complete_task(task.id))
        self.assertTrue(self.run_async(self.conductor.close_pane(task.id, "finished")))
        self.assertIn(("destroy", sid), self.runtime.calls)
        self.assertNotIn(task.id, self.workspaces.workspaces)
        released = self.emitted("workspace.released")
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0].data["reason"], "finished")
        self.assertEqual(released[0].task_id, task.id)
        self.assertEqual(len(self.emitted("task.pane_closed")), 1)
        types = [e["type"] for e in task_events.read(self.store, task.id)]
        self.assertEqual(types[-1], "completed")
        # Closed is closed: a straggling turn end must not reopen it.
        self.runtime.emit(sid, AgentEvent(type="completed", summary="late"))
        self.assertEqual(self.store.get(task.id).status, "completed")

    def test_closing_is_not_a_turn_end(self):
        """task.completed is a worker's answer and carries a notification.
        The user closing a task is a different event, or every close
        would pop a 'Completed' card for work they just said was over."""
        task = self.run_async(self.conductor.create_task("t", "g"))
        self.run_async(self.conductor.complete_task(task.id))
        self.assertEqual(len(self.emitted("task.closed")), 1)
        self.assertFalse(self.emitted("task.completed"))

    def test_cancel_releases_the_workspace_too(self):
        task = self.run_async(self.conductor.create_task("t", "g"))
        self.run_async(self.conductor.cancel_task(task.id))
        self.assertEqual(self.store.get(task.id).status, "cancelled")
        self.assertNotIn(task.id, self.workspaces.workspaces)
        self.assertEqual(self.emitted("workspace.released")[0].data["reason"],
                         "cancelled")

    def test_uncommitted_work_is_kept_and_reported(self):
        """cleanup() refuses a dirty worktree, and the refusal is the point:
        the branch holds committed work, the directory alone holds the
        rest, and a task ending is not permission to throw it away. But
        kept silently is how they accumulated - so it is reported."""
        from conductor.testing import FakeWorkspaceManager
        self.workspaces = FakeWorkspaceManager(fail_cleanup=True)
        self.conductor = Conductor(self.tmp.name, self.runtime,
                                   workspaces=self.workspaces, bus=self.bus)
        self.store = self.conductor.store
        task = self.run_async(self.conductor.create_task("t", "g"))
        self.run_async(self.conductor.complete_task(task.id))
        self.assertEqual(self.store.get(task.id).status, "completed")
        # Completion keeps the directory on purpose; the teardown is
        # where a dirty one is refused and reported.
        self.run_async(self.conductor.close_pane(task.id, "finished"))
        self.assertIn(task.id, self.workspaces.workspaces)
        kept = self.emitted("workspace.kept")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].severity, "warning")
        self.assertEqual(kept[0].data["reason"], "finished")
        self.assertIn("cleanup failed", kept[0].data["error"])
        self.assertFalse(self.emitted("workspace.released"))

    def test_a_task_that_never_started_leaves_no_workspace_behind(self):
        """No worker ever ran here, so there is nothing to come back to.
        These empty directories were the most common kind left over."""
        self.runtime = FakeRuntime(fail_create=True)
        self.conductor = Conductor(self.tmp.name, self.runtime,
                                   workspaces=self.workspaces, bus=self.bus)
        self.store = self.conductor.store
        with self.assertRaises(RuntimeError):
            self.run_async(self.conductor.create_task("t", "g"))
        task = self.store.list()[0]
        self.assertEqual(task.status, "failed")
        self.assertNotIn(task.id, self.workspaces.workspaces)
        self.assertEqual(self.emitted("workspace.released")[0].data["reason"],
                         "failed_to_start")

    def test_a_failed_start_that_is_still_hosted_keeps_its_workspace(self):
        """Measured 2026-08-29: a launch reported failed had in fact
        started - claude was running in the pane - and releasing the
        workspace deleted the worktree out from under it."""
        class Hosting(FakeRuntime):
            def session_alive(self, name):
                return True
        self.runtime = Hosting(fail_create=True)
        self.conductor = Conductor(self.tmp.name, self.runtime,
                                   workspaces=self.workspaces, bus=self.bus)
        self.store = self.conductor.store
        with self.assertRaises(RuntimeError):
            self.run_async(self.conductor.create_task("t", "g"))
        task = self.store.list()[0]
        self.assertEqual(task.status, "failed")
        self.assertIn(task.id, self.workspaces.workspaces)
        self.assertFalse(self.emitted("workspace.released"))
        kept = self.emitted("workspace.kept")
        self.assertEqual(kept[0].data["reason"], "failed_to_start")

    def test_a_worker_that_died_keeps_its_workspace_for_recovery(self):
        """A crash after the worker started is a different case: recovery
        resumes the session in this directory, so it has to be there."""
        task = self.run_async(self.conductor.create_task("t", "g"))
        self.runtime.emit(task.provider_session_id,
                          AgentEvent(type="failed", error="tmux session ended"))
        self.assertEqual(self.store.get(task.id).status, "failed")
        self.assertIn(task.id, self.workspaces.workspaces)
        self.assertFalse(self.emitted("workspace.released"))

    def test_complete_is_idempotent(self):
        """The manager prompt promises repeated commands are safe."""
        task = self.run_async(self.conductor.create_task("t", "g"))
        self.run_async(self.conductor.complete_task(task.id))
        self.run_async(self.conductor.complete_task(task.id))
        self.assertEqual(self.store.get(task.id).status, "completed")

    def test_complete_is_a_manager_tool(self):
        """The whole point: it has to be reachable from 'that's done'."""
        from conductor.global_conductor import MANAGER_TOOLS, MUTATING_TOOLS
        self.assertIn("complete_task", MANAGER_TOOLS)
        self.assertIn("complete_task", MUTATING_TOOLS)
        result = self.run_async(self.conductor.create_task("t", "g"))
        self.run_async(self.conductor.handle_action(
            "complete_task", {"task_id": result.id}))
        self.assertEqual(self.store.get(result.id).status, "completed")


class RecoveryTest(unittest.TestCase):
    def test_startup_wakes_nothing_by_default(self) -> None:
        """Launching must not spawn workers: every finished turn leaves a
        task in waiting_for_user, so resuming that state at startup revived
        every task ever created."""
        with tempfile.TemporaryDirectory() as tmp:
            conductor = Conductor(tmp, FakeRuntime())
            task = asyncio.run(conductor.create_task("t", "g"))

            revived = Conductor(tmp, FakeRuntime())
            self.assertEqual(asyncio.run(revived.startup()), [])
            self.assertEqual(revived.store.get(task.id).status,
                             conductor.store.get(task.id).status)

    def test_dormant_task_wakes_on_demand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conductor = Conductor(tmp, FakeRuntime())
            task = asyncio.run(conductor.create_task("t", "g"))

            revived = Conductor(tmp, FakeRuntime())
            asyncio.run(revived.startup())          # nothing running
            asyncio.run(revived.send_to_task(task.id, "carry on"))
            self.assertEqual(revived.store.get(task.id).status, "running")

    def test_startup_reconnects_or_marks_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime()
            conductor = Conductor(tmp, runtime)
            task = asyncio.run(conductor.create_task("t", "g"))

            # Simulate a restart: fresh Conductor over the same state dir.
            revived = Conductor(tmp, FakeRuntime())
            recovered = asyncio.run(revived.startup(resume=True))
            self.assertEqual([t.id for t in recovered], [task.id])

            # And a restart where the provider session is gone for good.
            broken = Conductor(tmp, FakeRuntime(fail_resume=True))
            self.assertEqual(asyncio.run(broken.startup(resume=True)), [])
            after = broken.store.get(task.id)
            self.assertEqual(after.status, "waiting_for_user")
            types = [e["type"]
                     for e in task_events.read(broken.store, task.id)]
            self.assertIn("session_lost", types)


if __name__ == "__main__":
    unittest.main()
