"""Boundary, privacy, scale, and resilience tests from the updated test plan.

The main rule under test (plan section 52): filesystem -> deterministic
discovery -> bounded candidates -> Manager selection -> resolved project ->
worker file discovery. A test here fails hard if the architecture degrades
into "dump the filesystem into the Manager prompt".

Run with:  python3 -m unittest tests.test_boundaries -v
"""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from conductor.global_conductor import GlobalConductor
from conductor.locator import MAX_CANDIDATES, ProjectLocator
from conductor.projects import ProjectStore
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager

# The complete set of keys a project candidate may expose to the Manager.
CANDIDATE_KEYS = {"path", "name", "markers", "repo_root", "remote_url",
                  "package_name", "registered_id", "score"}


def make_repo(path: Path, remote: str | None = None) -> Path:
    (path / ".git").mkdir(parents=True)
    if remote:
        (path / ".git" / "config").write_text(
            f'[remote "origin"]\n\turl = {remote}\n')
    return path


class World(unittest.TestCase):
    """Shared fixture: a conductor over a temp home and search root."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.roots = base / "code"
        self.roots.mkdir(parents=True)
        self.runtime = FakeCodingAgentRuntime()
        self.conductor = GlobalConductor(
            home=self.home, runtime=self.runtime,
            search_roots=[self.roots],
            workspace_factory=lambda project: FakeWorkspaceManager())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)


class LeakageTest(World):
    """Plan sections 11-12: the Manager receives bounded metadata only."""

    def test_candidates_expose_only_whitelisted_metadata(self) -> None:
        repo = make_repo(self.roots / "posely")
        (repo / "secrets.env").write_text("API_KEY=hunter2\n")
        (repo / "src").mkdir()
        (repo / "src" / "auth.ts").write_text("const token = 'abc';\n")
        for candidate in self.conductor.find_project("posely"):
            self.assertLessEqual(set(candidate), CANDIDATE_KEYS)
            blob = str(candidate)
            self.assertNotIn("auth.ts", blob)      # no file listings
            self.assertNotIn("hunter2", blob)      # no file contents

    def test_manager_context_never_contains_source_or_unrelated_paths(self):
        repo = make_repo(self.roots / "posely")
        (repo / "passwords.txt").write_text("do not leak\n")
        self.conductor.locator.register(repo)
        context = self.conductor.global_context()
        self.assertNotIn("passwords.txt", context)
        self.assertNotIn("do not leak", context)

    def test_search_results_are_bounded(self) -> None:
        for i in range(20):
            make_repo(self.roots / f"posely-{i}")
        self.assertLessEqual(len(self.conductor.find_project("posely")),
                             MAX_CANDIDATES)


class ScaleTest(World):
    """Plan sections 28-29: context and candidates stay bounded as the
    registry and index grow."""

    def test_manager_context_sublinear_in_projects(self) -> None:
        for i in range(300):
            path = self.roots / f"proj{i}"
            path.mkdir()
            self.conductor.projects.register(f"Project {i}", path)
        context = self.conductor.global_context()
        self.assertLess(len(context.splitlines()), 20)
        self.assertIn("more registered projects", context)

    def test_active_work_survives_the_context_cap(self) -> None:
        first = self.conductor.projects.register("Busy",
                                                 self.roots / "busy")
        (self.roots / "busy").mkdir()
        pc = self.conductor._conductor(first.id)
        task = pc.store.create("Fix login", "goal")
        pc.store.update(task.id, status="running",
                        provider_session_id="sess_x")
        for i in range(50):
            path = self.roots / f"idle{i}"
            path.mkdir()
            self.conductor.projects.register(f"Idle {i}", path)
        # "Busy" is now far outside the recency window, but has active work.
        self.assertIn("Busy", self.conductor.global_context())

    def test_large_index_still_returns_bounded_set(self) -> None:
        locator = self.conductor.locator
        from conductor.locator import ProjectCandidate
        for i in range(1000):
            locator.index.upsert(ProjectCandidate(
                path=f"/fake/repo{i}", name=f"repo{i}"))
        results = locator.search("repo")
        self.assertLessEqual(len(results), MAX_CANDIDATES)


class ResilienceTest(World):
    """Plan sections 30-31, 34: discovery survives bad filesystem states."""

    def test_unreadable_directory_does_not_break_discovery(self) -> None:
        make_repo(self.roots / "posely")
        locked = self.roots / "locked"
        locked.mkdir()
        os.chmod(locked, 0o000)
        try:
            self.assertEqual(len(self.conductor.find_project("posely")), 1)
        finally:
            os.chmod(locked, 0o755)

    def test_broken_symlink_is_skipped(self) -> None:
        make_repo(self.roots / "posely")
        (self.roots / "dangling").symlink_to(self.roots / "nowhere")
        self.assertEqual(len(self.conductor.find_project("posely")), 1)

    def test_external_drive_cycle_preserves_identity(self) -> None:
        drive = Path(self.tmp.name) / "Volumes" / "USB"
        repo = make_repo(drive / "posely")
        project = self.conductor.locator.register(repo)
        shutil.rmtree(drive)                     # drive unplugged
        report = self.run_async(self.conductor.startup())
        self.assertIn(project.id, report["missing_projects"])
        make_repo(drive / "posely")              # drive returns
        refreshed = self.conductor.locator.refresh(project.id)
        self.assertEqual(refreshed.status, "available")
        self.assertEqual(refreshed.id, project.id)

    def test_stale_path_refuses_task_creation(self) -> None:
        repo = make_repo(self.roots / "posely")
        project = self.conductor.locator.register(repo)
        shutil.rmtree(repo)
        with self.assertRaises(RuntimeError):
            self.run_async(self.conductor.create_task(
                "t", "g", project_id=project.id))


class MoveRecoveryTest(World):
    """Plan sections 9-10: unique moves reconnect; ambiguity never rewrites."""

    def _register_then_move(self, copies: int) -> tuple:
        old = make_repo(self.roots / "old" / "posely",
                        remote="git@github.com:me/posely.git")
        project = self.conductor.locator.register(old)
        for i in range(copies):
            make_repo(self.roots / f"new{i}" / "posely",
                      remote="git@github.com:me/posely.git")
        shutil.rmtree(old)
        return project

    def test_unique_move_relocates_on_startup(self) -> None:
        project = self._register_then_move(copies=1)
        self.run_async(self.conductor.startup())
        after = self.conductor.projects.get(project.id)
        self.assertEqual(after.status, "available")
        self.assertIn("new0", after.root_path)
        self.assertEqual(after.id, project.id)   # identity survived

    def test_ambiguous_move_marks_never_rewrites(self) -> None:
        project = self._register_then_move(copies=2)
        old_path = project.root_path
        self.run_async(self.conductor.startup())
        after = self.conductor.projects.get(project.id)
        self.assertEqual(after.status, "moved")
        self.assertEqual(after.root_path, old_path)   # untouched


class MonorepoTest(unittest.TestCase):
    """Plan section 32: subproject roots inside one repository."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.company = base / "company"
        subprocess.run(["git", "-C", str(base), "init", "-b", "main",
                        "company"], capture_output=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(base)})
        for app in ("web", "mobile"):
            path = self.company / "apps" / app
            path.mkdir(parents=True)
            (path / "package.json").write_text(f'{{"name": "{app}"}}')
        subprocess.run(["git", "-C", str(self.company), "add", "-A"],
                       capture_output=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(base)})
        subprocess.run(["git", "-C", str(self.company), "commit", "-m", "x",
                        "--no-gpg-sign"], capture_output=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(base),
                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t"})
        self.store = ProjectStore(base / "home")
        self.locator = ProjectLocator(self.store, [base])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_subproject_keeps_semantic_root_and_repo_root(self) -> None:
        web = self.locator.register(self.company / "apps" / "web",
                                    display_name="Web App")
        self.assertIn("apps/web", web.root_path)
        self.assertEqual(Path(web.repo_root), self.company.resolve())
        mobile = self.locator.register(self.company / "apps" / "mobile",
                                       display_name="Mobile App")
        self.assertNotEqual(web.id, mobile.id)
        self.assertEqual(web.repo_root, mobile.repo_root)

    def test_worktree_derives_from_repo_root(self) -> None:
        from conductor.global_conductor import default_workspace_factory
        web = self.locator.register(self.company / "apps" / "web",
                                    display_name="Web App")
        manager = default_workspace_factory(self.store)(web)
        workspace = manager.create("task_x")
        self.assertEqual(workspace.isolation_type, "git-worktree")
        # The worktree contains the whole repo, including the subproject.
        self.assertTrue((Path(workspace.path) / "apps" / "web"
                         / "package.json").exists())
        manager.cleanup("task_x", force=True)


class CrossProjectIsolationTest(World):
    """Plan sections 24-25, 36: no cross-project contamination."""

    def _project_with_task(self, name: str):
        make_repo(self.roots / name)
        project = self.conductor.locator.register(self.roots / name)
        task = self.run_async(self.conductor.create_task(
            f"Task in {name}", "goal", project_id=project.id))
        return project, task

    def test_sessions_and_workspaces_stay_project_scoped(self) -> None:
        posely, task_a = self._project_with_task("posely")
        cheatly, task_b = self._project_with_task("cheatly")
        self.assertEqual(task_a.project_id, posely.id)
        self.assertEqual(task_b.project_id, cheatly.id)
        self.assertNotEqual(task_a.provider_session_id,
                            task_b.provider_session_id)
        # A message to B's task must reach B's session, never A's.
        self.run_async(self.conductor.send_to_task(task_b.id, "hello"))
        self.assertIn(("send", task_b.provider_session_id, "hello"),
                      self.runtime.calls)
        self.assertNotIn(("send", task_a.provider_session_id, "hello"),
                         self.runtime.calls)

    def test_multi_project_random_ops_keep_scoping(self) -> None:
        projects = []
        for name in ("alpha", "beta", "gamma"):
            make_repo(self.roots / name)
            projects.append(self.conductor.locator.register(
                self.roots / name))
        rng = random.Random(7)
        for step in range(60):
            project = rng.choice(projects)
            tasks = self.conductor.list_tasks(project.id)
            op = rng.choice(("create", "send", "interrupt", "cancel",
                             "restart"))
            try:
                if op == "create":
                    self.run_async(self.conductor.create_task(
                        f"t{step}", "g", project_id=project.id))
                elif op == "send" and tasks:
                    self.run_async(self.conductor.send_to_task(
                        rng.choice(tasks).id, "m"))
                elif op == "interrupt" and tasks:
                    self.run_async(self.conductor.interrupt_task(
                        rng.choice(tasks).id))
                elif op == "cancel" and tasks:
                    self.run_async(self.conductor.cancel_task(
                        rng.choice(tasks).id))
                elif op == "restart":
                    self.conductor = GlobalConductor(
                        home=self.home, runtime=self.runtime,
                        search_roots=[self.roots],
                        workspace_factory=lambda p: FakeWorkspaceManager())
                    self.run_async(self.conductor.startup())
            except RuntimeError:
                pass                             # cap reached etc. is fine
            # Invariants: every task belongs to its project, sessions unique.
            for project_check in projects:
                for task in self.conductor.list_tasks(project_check.id):
                    self.assertEqual(task.project_id, project_check.id)
            sessions = [t.provider_session_id
                        for t in self.conductor.list_tasks()
                        if t.provider_session_id
                        and t.status not in ("cancelled", "failed")]
            self.assertEqual(len(sessions), len(set(sessions)))

    def test_multi_project_restart_restores_everything(self) -> None:
        posely, task_a = self._project_with_task("posely")
        cheatly, task_b = self._project_with_task("cheatly")
        self.run_async(self.conductor.interrupt_task(task_b.id))

        revived = GlobalConductor(
            home=self.home, runtime=FakeCodingAgentRuntime(),
            search_roots=[self.roots],
            workspace_factory=lambda p: FakeWorkspaceManager())
        report = self.run_async(revived.startup(resume=True))
        by_id = {t.id: t for t in revived.list_tasks()}
        self.assertEqual(by_id[task_a.id].project_id, posely.id)
        self.assertEqual(by_id[task_b.id].status, "waiting_for_user")
        # Both sessions reconnect - a waiting task still needs its session
        # for the next follow-up; waiting is a task state, not session death.
        self.assertEqual({t.id for t in report["recovered_tasks"]},
                         {task_a.id, task_b.id})


if __name__ == "__main__":
    unittest.main()
