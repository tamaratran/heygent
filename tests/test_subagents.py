"""Managed-subagent and surface-model tests: structured results flow to the
Manager without transcript rereads, surfaces attach and never fork, the
transcript view is honestly read-only, and preference picks the richest
registered surface with the transcript as fallback.

Run with:  python3 -m unittest tests.test_subagents -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.agent_events import AgentEvent
from conductor.global_conductor import GlobalConductor
from conductor.subagents import SubagentResult, build_subagent
from conductor.surfaces import (FakeSurface, SurfaceHandle,
                                SurfacePreference, SurfaceRequest,
                                TranscriptSurface)
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


class SubagentWorld(unittest.TestCase):
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

    def make_task(self, title: str = "Fix login"):
        registered = self.conductor.locator.register(self.roots / "posely")
        return self.run_async(self.conductor.create_task(
            title, "goal", project_id=registered.id))


class ResultFlowTest(SubagentWorld):
    def test_result_is_structured_and_persisted(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        # The worker checkpoints, then finishes: the result combines both.
        self.runtime.emit(sid, AgentEvent(
            type="checkpoint", summary="Candidate fix in place",
            detail={"files_changed": ["src/auth/login.ts"],
                    "findings": ["Redirect raced the session refresh"]}))
        self.runtime.emit(sid, AgentEvent(
            type="completed", summary="Fixed the redirect; all tests pass."))
        subagent = self.run_async(self.conductor.subagent_for(task.id))
        self.assertIsNotNone(subagent.result)
        self.assertTrue(subagent.result.success)
        self.assertIn("all tests pass", subagent.result.summary)
        self.assertEqual(subagent.result.files_changed,
                         ["src/auth/login.ts"])
        self.assertEqual(subagent.result.findings,
                         ["Redirect raced the session refresh"])
        # Persisted: a fresh conductor over the same home still has it.
        revived = GlobalConductor(
            home=Path(self.tmp.name) / "home",
            runtime=FakeCodingAgentRuntime(),
            workspace_factory=lambda p: FakeWorkspaceManager())
        stored = {t.id: t for t in revived.list_tasks()}[task.id]
        self.assertTrue(SubagentResult.from_dict(stored.result).success)

    def test_manager_sees_result_via_inspect(self) -> None:
        task = self.make_task()
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="completed", summary="Done."))
        report = self.run_async(self.conductor.handle_action(
            "inspect_task", {"task_id": task.id}))
        self.assertEqual(report["result"]["summary"], "Done.")
        self.assertTrue(report["result"]["success"])

    def test_failed_worker_reports_unsuccessful_result(self) -> None:
        task = self.make_task()
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="failed", error="tests would not pass"))
        subagent = self.run_async(self.conductor.subagent_for(task.id))
        self.assertEqual(subagent.status, "failed")
        self.assertFalse(subagent.result.success)


class SupervisionViewTest(SubagentWorld):
    def test_status_vocabulary(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        self.assertEqual(self.run_async(
            self.conductor.subagent_for(task.id)).status, "working")
        self.runtime.emit_approval(sid, "appr_1", "install a package")
        self.assertEqual(self.run_async(
            self.conductor.subagent_for(task.id)).status,
            "waiting_for_approval")
        self.run_async(self.conductor.approve_task_action(task.id, "appr_1"))
        self.runtime.emit(sid, AgentEvent(type="needs_input",
                                          question="Which approach?"))
        self.assertEqual(self.run_async(
            self.conductor.subagent_for(task.id)).status,
            "waiting_for_input")

    def test_subagents_orders_attention_first(self) -> None:
        blocked = self.make_task("Blocked")
        self.runtime.emit_approval(blocked.provider_session_id, "appr_9",
                                   "risky thing")
        working = self.make_task("Working")
        views = self.run_async(self.conductor.subagents())
        self.assertEqual(views[0].task_id, blocked.id)
        self.assertEqual(views[0].status, "waiting_for_approval")

    def test_identity_is_stable_and_derived(self) -> None:
        task = self.make_task()
        first = build_subagent(task)
        again = build_subagent(task)
        self.assertEqual(first.id, again.id)
        self.assertEqual(first.id, f"sub_{task.id}")


class SurfaceModelTest(SubagentWorld):
    def test_surface_operations_never_create_executions(self) -> None:
        task = self.make_task()
        before = self.runtime._counter
        self.run_async(self.conductor.focus_task(task.id))
        stored = {t.id: t for t in self.conductor.list_tasks()}[task.id]
        # The user closes the window; focus must re-attach, never re-spawn.
        self.surface.close(SurfaceHandle.from_dict(stored.surface))
        self.run_async(self.conductor.focus_task(task.id))
        self.assertEqual(self.runtime._counter, before)   # zero new workers
        self.assertEqual(len(self.run_async(self.conductor.executions())), 1)

    def test_preference_picks_richest_then_falls_back(self) -> None:
        base = Path(self.tmp.name)
        interactive = FakeSurface()
        interactive.interactive = True
        transcript = FakeSurface()
        conductor = GlobalConductor(
            home=base / "home2",
            runtime=FakeCodingAgentRuntime(),
            search_roots=[self.roots],
            surfaces={"interactive-terminal": interactive,
                      "transcript": transcript},
            workspace_factory=lambda p: FakeWorkspaceManager())
        registered = conductor.locator.register(self.roots / "posely")
        task = self.run_async(conductor.create_task(
            "t", "g", project_id=registered.id))
        self.assertEqual(len(interactive.created), 1)     # preferred won
        self.assertEqual(transcript.created, [])
        handle = {t.id: t for t in conductor.list_tasks()}[task.id].surface
        self.assertEqual(handle["metadata"]["surface_name"],
                         "interactive-terminal")
        self.assertEqual(handle["metadata"]["interactive"], "true")

        # Only a transcript registered: the fallback carries the task.
        fallback_only = GlobalConductor(
            home=base / "home3",
            runtime=FakeCodingAgentRuntime(),
            search_roots=[self.roots],
            surfaces={"transcript": transcript},
            workspace_factory=lambda p: FakeWorkspaceManager())
        registered = fallback_only.locator.register(self.roots / "posely")
        self.run_async(fallback_only.create_task(
            "t", "g", project_id=registered.id))
        self.assertEqual(len(transcript.created), 1)

    def test_transcript_surface_is_honestly_read_only(self) -> None:
        surface = TranscriptSurface()
        self.assertFalse(surface.interactive)
        request = SurfaceRequest(
            project_id="proj_x", task_id="task_x",
            title="posely — Fix login — Claude Code",
            working_directory="/tmp/ws", provider="claude-code",
            provider_session_id="sess_1",
            transcript_path="/tmp/executions/task_x.log")
        command = surface.command_for(request)
        self.assertIn("READ-ONLY", command)
        self.assertIn("tail -n +1 -f", command)
        self.assertNotIn("claude -r", command)     # attach, never fork
        with self.assertRaises(RuntimeError):
            surface.create(SurfaceRequest(
                project_id="p", task_id="t", title="x",
                working_directory="/", provider="claude-code"))

    def test_default_preference_order(self) -> None:
        preference = SurfacePreference()
        self.assertEqual(preference.order_for("claude-code")[0],
                         "interactive-terminal")
        self.assertEqual(preference.order_for("codex")[0], "codex-app")

    def test_no_read_only_fallback_is_offered(self) -> None:
        """A tail window shows the right session but cannot be typed into,
        so offering it as a last resort made a failed attachment look like a
        working session."""
        preference = SurfacePreference()
        for provider in ("claude-code", "codex"):
            self.assertNotIn("transcript", preference.order_for(provider))


if __name__ == "__main__":
    unittest.main()
