"""create_task resolves the project itself: a name, a path or an id in one
call, where the Boss used to spend three tool turns (find_project,
register_project, create_task) before any work started.

Run with:  python3 -m unittest tests.test_project_resolution -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.global_conductor import GlobalConductor
from conductor.manager import FakeManagerBackend
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


def make_repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


class ProjectResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = base / "code"
        make_repo(self.roots / "posely")
        make_repo(self.roots / "cheatly")
        self.conductor = GlobalConductor(
            home=base / "home", runtime=FakeCodingAgentRuntime(),
            manager=FakeManagerBackend(), search_roots=[self.roots],
            workspace_factory=lambda project: FakeWorkspaceManager())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_a_name_registers_and_creates_in_one_call(self) -> None:
        task = self.run_async(self.conductor.handle_action(
            "create_task", {"project": "Posely", "title": "Fix login",
                            "goal": "Fix the login bug"}))
        (project,) = self.conductor.projects.list()
        self.assertEqual(project.display_name, "posely")
        self.assertEqual(task.project_id, project.id)
        self.assertEqual(task.status, "running")

    def test_a_registered_name_resolves_without_reregistering(self) -> None:
        project = self.conductor.locator.register(self.roots / "posely",
                                                  display_name="Posely")
        task = self.run_async(self.conductor.create_task(
            "Fix login", "goal", project="posely"))
        self.assertEqual(task.project_id, project.id)
        self.assertEqual(len(self.conductor.projects.list()), 1)

    def test_a_path_registers_and_creates(self) -> None:
        task = self.run_async(self.conductor.create_task(
            "Fix login", "goal", project=str(self.roots / "cheatly")))
        (project,) = self.conductor.projects.list()
        # Resolved: on macOS the temp dir is /var/..., a symlink to
        # /private/var/..., and the registry stores the real path.
        self.assertEqual(project.root_path,
                         str((self.roots / "cheatly").resolve()))
        self.assertEqual(task.project_id, project.id)

    def test_a_project_id_is_accepted_as_the_reference(self) -> None:
        project = self.conductor.locator.register(self.roots / "posely")
        task = self.run_async(self.conductor.create_task(
            "Fix login", "goal", project=project.id))
        self.assertEqual(task.project_id, project.id)

    def test_an_ambiguous_name_names_the_candidates(self) -> None:
        make_repo(self.roots / "deeper" / "app")
        make_repo(self.roots / "elsewhere" / "app")
        with self.assertRaises(ValueError) as caught:
            self.conductor.resolve_project("app")
        message = str(caught.exception)
        self.assertIn("more than one project", message)
        self.assertIn("app", message)

    def test_a_spoken_name_finds_the_hyphenated_directory(self) -> None:
        """Queries arrive by voice, without punctuation. "voice agent"
        failed to find the voice-agent repo twice in a live run before
        the Boss fell back to a full path."""
        make_repo(self.roots / "voice-agent")
        project = self.conductor.resolve_project("voice agent")
        self.assertEqual(project.root_path,
                         str((self.roots / "voice-agent").resolve()))
        registered = self.conductor.resolve_project("Voice Agent")
        self.assertEqual(registered.id, project.id)

    def test_an_unknown_name_asks_for_a_path(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.conductor.resolve_project("nonesuch")
        self.assertIn("no project matching", str(caught.exception))

    def test_a_missing_path_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.conductor.resolve_project(str(self.roots / "gone"))

    def test_explicit_project_id_still_wins(self) -> None:
        project = self.conductor.locator.register(self.roots / "posely")
        task = self.run_async(self.conductor.create_task(
            "Fix login", "goal", project_id=project.id, project="cheatly"))
        self.assertEqual(task.project_id, project.id)


if __name__ == "__main__":
    unittest.main()
