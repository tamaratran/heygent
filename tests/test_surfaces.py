"""Session surface tests: visible defaults, background mode, focus by id,
recovery, restart reconciliation, wrong-surface protection. All against
FakeSurface - no AppleScript, no model.

Run with:  python3 -m unittest tests.test_surfaces -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.global_conductor import GlobalConductor
from conductor.surfaces import FakeSurface, SurfaceHandle
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


class SurfaceWorld(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = base / "code"
        for name in ("posely", "cheatly"):
            (self.roots / name / ".git").mkdir(parents=True)
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

    def make_task(self, project: str, title: str, background: bool = False):
        registered = self.conductor.locator.register(self.roots / project)
        return self.run_async(self.conductor.create_task(
            title, "goal", project_id=registered.id, background=background))


class VisibleCreationTest(SurfaceWorld):
    def test_user_task_gets_visible_surface_by_default(self) -> None:
        task = self.make_task("posely", "Fix login redirect")
        self.assertEqual(task.surface_mode, "visible")
        self.assertIsNotNone(task.surface)
        request = self.surface.created[0]
        self.assertEqual(request.task_id, task.id)
        self.assertEqual(request.provider_session_id,
                         task.provider_session_id)
        # The surface opens in the task's workspace, titled with context.
        self.assertEqual(request.working_directory, task.workspace.path)
        self.assertIn("posely", request.title)
        self.assertIn("Fix login redirect", request.title)
        self.assertIn("Claude Code", request.title)

    def test_multiple_tasks_get_independent_surfaces(self) -> None:
        a = self.make_task("posely", "A")
        b = self.make_task("cheatly", "B")
        self.assertEqual(len(self.surface.created), 2)
        self.assertNotEqual(a.surface["id"], b.surface["id"])

    def test_surface_failure_never_fails_the_task(self) -> None:
        def boom(request):
            raise RuntimeError("no display")
        self.surface.create = boom
        task = self.make_task("posely", "Fix login")
        self.assertEqual(task.status, "running")
        self.assertIsNone(task.surface)

    def test_background_task_gets_no_surface(self) -> None:
        task = self.make_task("posely", "Quiet investigation",
                              background=True)
        self.assertEqual(task.surface_mode, "background")
        self.assertIsNone(task.surface)
        self.assertEqual(self.surface.created, [])
        self.assertEqual(task.status, "running")   # still runs and reports


class FocusTest(SurfaceWorld):
    def test_focus_brings_existing_surface_forward(self) -> None:
        task = self.make_task("posely", "Fix login")
        self.run_async(self.conductor.focus_task(task.id))
        self.assertEqual(self.surface.focused, [task.surface["id"]])
        # Navigation never messages the worker.
        self.assertFalse([c for c in self.runtime.calls
                          if c[0] == "send"])

    def test_focus_by_task_id_with_identical_titles(self) -> None:
        a = self.make_task("posely", "Fix login")
        b = self.make_task("cheatly", "Fix login")
        self.run_async(self.conductor.focus_task(b.id))
        self.assertEqual(self.surface.focused, [b.surface["id"]])

    def test_closed_surface_is_recovered(self) -> None:
        task = self.make_task("posely", "Fix login")
        old_id = task.surface["id"]
        self.surface.close(SurfaceHandle.from_dict(task.surface))
        self.run_async(self.conductor.focus_task(task.id))
        refreshed = next(t for t in self.conductor.list_tasks()
                         if t.id == task.id)
        self.assertNotEqual(refreshed.surface["id"], old_id)
        # The recovered surface resumes the same provider session.
        self.assertEqual(self.surface.created[-1].provider_session_id,
                         task.provider_session_id)

    def test_background_task_becomes_visible_on_demand(self) -> None:
        task = self.make_task("posely", "Quiet work", background=True)
        self.run_async(self.conductor.focus_task(task.id))
        refreshed = next(t for t in self.conductor.list_tasks()
                         if t.id == task.id)
        self.assertEqual(refreshed.surface_mode, "visible")
        self.assertIsNotNone(refreshed.surface)

    def test_headless_conductor_reports_no_surface(self) -> None:
        headless = GlobalConductor(
            home=Path(self.tmp.name) / "home2",
            runtime=FakeCodingAgentRuntime(),
            workspace_factory=lambda project: FakeWorkspaceManager())
        registered = headless.locator.register(self.roots / "posely")
        task = self.run_async(headless.create_task(
            "t", "g", project_id=registered.id))
        with self.assertRaises(RuntimeError):
            self.run_async(headless.focus_task(task.id))


class RestartTest(SurfaceWorld):
    def test_restart_reconciles_stale_handles_without_reopening(self) -> None:
        alive = self.make_task("posely", "Alive")
        dead = self.make_task("cheatly", "Dead")
        self.surface.close(SurfaceHandle.from_dict(dead.surface))

        revived = GlobalConductor(
            home=Path(self.tmp.name) / "home",
            runtime=FakeCodingAgentRuntime(),
            search_roots=[self.roots], surface=self.surface,
            workspace_factory=lambda project: FakeWorkspaceManager())
        opened_before = len(self.surface.created)
        self.run_async(revived.startup())
        by_id = {t.id: t for t in revived.list_tasks()}
        self.assertIsNotNone(by_id[alive.id].surface)     # kept
        self.assertIsNone(by_id[dead.id].surface)         # cleared
        # No historical windows were spawned on startup.
        self.assertEqual(len(self.surface.created), opened_before)


if __name__ == "__main__":
    unittest.main()
