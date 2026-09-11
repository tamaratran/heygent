"""One task, one execution, one visible surface rendering THAT execution.

The bug under test: a hidden worker doing the work while an unrelated
visible session sits in the terminal. Prohibited at the runtime level and
verified end to end: the surface's transcript path is the execution's
transcript path, the prompt and every follow-up appear in that one stream,
and completion propagates from that exact worker.

Run with:  python3 -m unittest tests.test_execution -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.agent_events import AgentEvent
from conductor.global_conductor import GlobalConductor
from conductor.notifications import NotificationService
from conductor.surfaces import FakeSurface
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


class ExecutionWorld(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = base / "code"
        (self.roots / "posely" / ".git").mkdir(parents=True)
        self.runtime = FakeCodingAgentRuntime(
            transcript_dir=str(base / "home" / "executions"))
        self.surface = FakeSurface()
        self.conductor = GlobalConductor(
            home=base / "home", runtime=self.runtime,
            search_roots=[self.roots], surface=self.surface,
            workspace_factory=lambda project: FakeWorkspaceManager())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def make_task(self, title: str = "Create conductor-test.txt"):
        registered = self.conductor.locator.register(self.roots / "posely")
        return self.run_async(self.conductor.create_task(
            title, "Create a file called conductor-test.txt containing "
            "hello", project_id=registered.id))

    def transcript(self, task_id: str) -> str:
        return self.runtime.transcript.path(task_id).read_text()


class OneExecutionTest(ExecutionWorld):
    def test_runtime_prohibits_second_execution_per_task(self) -> None:
        task = self.make_task()
        with self.assertRaises(RuntimeError):
            self.run_async(self.runtime.create_session(
                task.id, "/anywhere", "sneaky second worker"))
        executions = self.run_async(self.conductor.executions())
        self.assertEqual(len(executions), 1)

    def test_execution_mapping_is_complete_and_logged(self) -> None:
        events = []
        self.conductor.bus.subscribe(events.append)
        task = self.make_task()
        execution = self.run_async(self.conductor.executions())[0]
        self.assertEqual(execution.task_id, task.id)
        self.assertEqual(execution.provider_session_id,
                         task.provider_session_id)
        self.assertEqual(execution.workspace_path, task.workspace.path)
        self.assertIsNotNone(execution.transcript_path)
        mapped = [e for e in events if e.type == "execution.mapped"]
        self.assertEqual(mapped[0].provider_session_id,
                         task.provider_session_id)


class VisibleExecutionTest(ExecutionWorld):
    def test_surface_renders_the_exact_execution(self) -> None:
        task = self.make_task()
        request = self.surface.created[0]
        execution = self.run_async(self.conductor.executions())[0]
        # The terminal tails the same file the execution writes: by
        # construction, what the user watches IS the working session.
        self.assertEqual(request.transcript_path,
                         execution.transcript_path)
        self.assertEqual(request.provider_session_id,
                         execution.provider_session_id)
        body = self.transcript(task.id)
        self.assertIn(f"session {task.provider_session_id}", body)
        self.assertIn("conductor-test.txt", body)     # the prompt, visibly

    def test_follow_up_lands_in_the_same_visible_stream(self) -> None:
        task = self.make_task()
        self.run_async(self.conductor.send_to_task(
            task.id, "Now change hello to hello world."))
        body = self.transcript(task.id)
        self.assertIn("Now change hello to hello world.", body)
        # Same session, same terminal: nothing new was created.
        self.assertEqual(len(self.surface.created), 1)
        self.assertEqual(len(self.run_async(self.conductor.executions())), 1)
        self.assertEqual(self.runtime._counter, 1)

    def test_completion_comes_from_the_visible_worker(self) -> None:
        task = self.make_task()
        popups = []
        service = NotificationService(self.conductor,
                                      on_notify=popups.append)
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="completed",
            summary="Done. Created conductor-test.txt with hello."))
        # The completion of the session the user watched drives everything:
        state = {t.id: t for t in self.conductor.list_tasks()}[task.id]
        self.assertEqual(state.status, "waiting_for_user")
        self.assertIn("completed", [p.type for p in popups])
        completions = [n for n in service.store.list(task_id=task.id)
                       if n.type == "completed"]
        self.assertEqual(len(completions), 1)
        service.close()

    def test_reconstruction_keeps_one_stream(self) -> None:
        task = self.make_task()
        self.runtime.vanish(task.provider_session_id)
        self.runtime.fail_resume = True
        self.run_async(self.conductor.recover_task(task.id))
        executions = self.run_async(self.conductor.executions())
        self.assertEqual(len(executions), 1)           # still exactly one
        refreshed = {t.id: t for t in self.conductor.list_tasks()}[task.id]
        self.assertEqual(executions[0].provider_session_id,
                         refreshed.provider_session_id)
        # The replacement continues the SAME visible stream, new header
        # announcing the new session id.
        body = self.transcript(task.id)
        self.assertIn(f"session {refreshed.provider_session_id}", body)


if __name__ == "__main__":
    unittest.main()
