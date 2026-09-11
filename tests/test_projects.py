"""Project store and locator tests: registry, focus, discovery, duplicates,
moved projects, worktree filtering. No models.

Run with:  python3 -m unittest tests.test_projects -v
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from conductor.locator import ProjectLocator
from conductor.projects import ProjectStore


def make_repo(path: Path, remote: str | None = None,
              package: str | None = None) -> Path:
    (path / ".git").mkdir(parents=True)
    if remote:
        (path / ".git" / "config").write_text(
            f'[remote "origin"]\n\turl = {remote}\n')
    if package:
        (path / "package.json").write_text(json.dumps({"name": package}))
    return path


class ProjectStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.store = ProjectStore(self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_register_get_list(self) -> None:
        project = self.store.register("Posely", self.tmp.name,
                                      aliases=["pose"])
        self.assertTrue(project.id.startswith("proj_"))
        self.assertEqual(self.store.get(project.id).display_name, "Posely")
        self.assertEqual([p.id for p in self.store.list()], [project.id])

    def test_a_null_focus_on_disk_is_healed_not_trusted(self) -> None:
        """Measured on 2026-09-01: global.json held "focus": null, and
        every manager tool died at set_focus with 'NoneType' object
        does not support item assignment - the Boss, told create_task
        failed, started three workers for one request."""
        home = Path(self.tmp.name) / "healed"
        home.mkdir()
        (home / "global.json").write_text(json.dumps(
            {"version": 1, "focus": None, "manager": None}))
        store = ProjectStore(home)
        store.set_focus(project_id="proj_x", task_id="task_y")
        self.assertEqual(store.focus()["task_id"], "task_y")
        self.assertIsNone(store.manager()["provider"])
        self.assertEqual(store.recent_ids(), [])   # missing key, healed

    def test_identity_survives_path_change(self) -> None:
        project = self.store.register("Posely", self.tmp.name)
        moved = self.store.update(project.id, root_path="/new/place",
                                  status="moved")
        self.assertEqual(moved.id, project.id)     # Invariant 16
        self.assertEqual(ProjectStore(self.home).get(project.id).root_path,
                         "/new/place")

    def test_find_by_name_uses_aliases(self) -> None:
        self.store.register("Voice Conductor", self.tmp.name,
                            aliases=["voice agent", "conductor"])
        self.assertEqual(len(self.store.find_by_name("voice agent")), 1)
        self.assertEqual(len(self.store.find_by_name("conductor")), 1)
        self.assertEqual(self.store.find_by_name("nonexistent"), [])

    def test_recency_and_focus_persist(self) -> None:
        a = self.store.register("A", self.tmp.name)
        b = self.store.register("B", self.tmp.name)
        self.store.touch(a.id)
        self.store.set_focus(project_id=a.id, task_id="task_x")
        reloaded = ProjectStore(self.home)
        self.assertEqual(reloaded.recent_ids()[0], a.id)
        self.assertEqual(reloaded.focus(),
                         {"project_id": a.id, "task_id": "task_x"})


class LocatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.roots = base / "code"
        self.store = ProjectStore(self.home)
        self.locator = ProjectLocator(self.store, [self.roots])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_discovers_and_registers(self) -> None:
        make_repo(self.roots / "posely", remote="git@github.com:me/posely.git")
        candidates = self.locator.search("posely")
        self.assertEqual(len(candidates), 1)
        self.assertIsNone(candidates[0].registered_id)
        project = self.locator.register(candidates[0].path)
        self.assertEqual(project.display_name, "posely")
        self.assertEqual(project.remote_url, "git@github.com:me/posely.git")
        # Registration is idempotent, and registered wins future searches.
        again = self.locator.register(candidates[0].path)
        self.assertEqual(again.id, project.id)
        self.assertEqual(self.locator.search("posely")[0].registered_id,
                         project.id)

    def test_duplicate_folder_names_both_surface(self) -> None:
        make_repo(self.roots / "active" / "posely",
                  remote="git@github.com:me/posely.git")
        make_repo(self.roots / "archive" / "posely",
                  remote="git@github.com:me/posely.git")
        candidates = self.locator.search("posely")
        self.assertEqual(len(candidates), 2)   # never silently pick by name

    def test_linked_worktrees_are_not_projects(self) -> None:
        repo = make_repo(self.roots / "posely")
        worktree = self.roots / "posely-wt"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {repo}/.git/worktrees/x\n")
        self.assertIsNone(self.locator.inspect_path(worktree))

    def test_the_mac_homes_library_is_never_scanned(self) -> None:
        user_home = Path(self.tmp.name) / "me"
        make_repo(user_home / "Library" / "Caches" / "some-repo")
        make_repo(user_home / "code" / "Library" / "real-repo")
        make_repo(user_home / "code" / "app")
        locator = ProjectLocator(self.store, [user_home])
        with mock.patch.object(Path, "home", return_value=user_home):
            locator.scan()
        names = sorted(Path(c.path).name for c in locator.index.entries())
        self.assertEqual(names, ["app", "real-repo"])

    def test_own_home_is_never_scanned(self) -> None:
        make_repo(self.home / "workspaces" / "proj_x" / "task_y")
        locator = ProjectLocator(self.store, [self.home])
        self.assertEqual(locator.search("task_y"), [])

    def test_moved_project_found_by_fingerprint(self) -> None:
        old = make_repo(self.roots / "old-spot" / "posely",
                        remote="git@github.com:me/posely.git",
                        package="posely")
        project = self.locator.register(old)
        # The repo moves; the old path is gone.
        new = make_repo(self.roots / "new-spot" / "posely-renamed",
                        remote="git@github.com:me/posely.git",
                        package="posely")
        import shutil
        shutil.rmtree(old)
        self.assertEqual(self.locator.refresh(project.id).status, "missing")
        matches = self.locator.find_moved(project.id)
        self.assertEqual(len(matches), 1)
        self.assertEqual(Path(matches[0].path), new.resolve())
        # Reconnect identity: same id, new path.
        self.store.update(project.id, root_path=matches[0].path,
                          status="available")
        self.assertEqual(self.locator.refresh(project.id).status, "available")

    def test_search_matches_package_name(self) -> None:
        make_repo(self.roots / "webapp", package="@acme/storefront")
        self.assertEqual(len(self.locator.search("storefront")), 1)


class IndexTest(unittest.TestCase):
    """Discovery is cached and incremental: the filesystem is rescanned only
    when a name cannot be resolved from the registry or index."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.roots = base / "code"
        self.store = ProjectStore(self.home)
        self.locator = ProjectLocator(self.store, [self.roots])
        self.walks = 0
        original = self.locator._walk

        def counting_walk():
            self.walks += 1
            return original()
        self.locator._walk = counting_walk

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_second_search_hits_the_cache(self) -> None:
        make_repo(self.roots / "posely")
        self.assertEqual(len(self.locator.search("posely")), 1)
        self.assertEqual(self.walks, 1)          # cold: one scan
        self.assertEqual(len(self.locator.search("posely")), 1)
        self.assertEqual(self.walks, 1)          # warm: no rescan

    def test_registered_project_never_triggers_a_scan(self) -> None:
        repo = make_repo(self.roots / "posely")
        self.locator.register(repo)
        self.assertEqual(len(self.locator.search("posely")), 1)
        self.assertEqual(self.walks, 0)

    def test_unresolved_name_expands_the_search(self) -> None:
        make_repo(self.roots / "posely")
        self.locator.search("posely")
        make_repo(self.roots / "newling")        # appears after first scan
        self.assertEqual(len(self.locator.search("newling")), 1)
        self.assertEqual(self.walks, 2)          # unresolved -> rescan

    def test_index_persists_across_restarts(self) -> None:
        make_repo(self.roots / "posely")
        self.locator.scan()
        fresh = ProjectLocator(ProjectStore(self.home), [self.roots])
        self.assertEqual(len(fresh.search("posely")), 1)

    def test_candidate_set_is_bounded(self) -> None:
        for i in range(8):
            make_repo(self.roots / f"posely-{i}")
        self.assertLessEqual(len(self.locator.search("posely")), 5)

    def test_vanished_entries_are_pruned(self) -> None:
        repo = make_repo(self.roots / "posely")
        self.locator.scan()
        import shutil
        shutil.rmtree(repo)
        self.assertEqual(self.locator.index.entries(), [])


if __name__ == "__main__":
    unittest.main()
