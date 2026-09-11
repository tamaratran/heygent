"""Phase 3 tests: workspace isolation with real git worktrees.

Run with:  python3 -m unittest tests.test_workspaces -v
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from conductor.workspaces import GitWorktreeManager, SharedWorkspaceManager


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "t",
             "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(repo)})
    assert result.returncode == 0, result.stderr
    return result.stdout


class GitWorktreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "app"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        (self.repo / "shared.txt").write_text("original\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "init", "--no-gpg-sign")
        self.manager = GitWorktreeManager(self.repo)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_two_tasks_are_isolated(self) -> None:
        a = self.manager.create("task_a")
        b = self.manager.create("task_b")
        self.assertNotEqual(a.path, b.path)
        self.assertEqual(a.isolation_type, "git-worktree")

        # Each worker edits the same file in its own workspace.
        (Path(a.path) / "shared.txt").write_text("from task a\n")
        (Path(b.path) / "shared.txt").write_text("from task b\n")

        self.assertEqual((Path(a.path) / "shared.txt").read_text(),
                         "from task a\n")
        self.assertEqual((Path(b.path) / "shared.txt").read_text(),
                         "from task b\n")
        # The main checkout never saw either change.
        self.assertEqual((self.repo / "shared.txt").read_text(), "original\n")

    def test_create_is_idempotent(self) -> None:
        first = self.manager.create("task_a")
        again = self.manager.create("task_a")
        self.assertEqual(first.path, again.path)

    def test_get_and_missing(self) -> None:
        self.manager.create("task_a")
        self.assertEqual(self.manager.get("task_a").branch, "agent/task_a")
        with self.assertRaises(KeyError):
            self.manager.get("task_missing")

    def test_cleanup_refuses_dirty_then_forces(self) -> None:
        ws = self.manager.create("task_a")
        (Path(ws.path) / "shared.txt").write_text("uncommitted work\n")
        with self.assertRaises(RuntimeError):
            self.manager.cleanup("task_a")           # dirty: refuse
        self.manager.cleanup("task_a", force=True)   # explicit force removes
        self.assertFalse(Path(ws.path).exists())
        # The branch survives cleanup; only the directory goes.
        branches = git(self.repo, "branch", "--list", "agent/task_a")
        self.assertIn("agent/task_a", branches)

    def test_rejects_non_repo(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(ValueError):
                GitWorktreeManager(other)


class SharedWorkspaceTest(unittest.TestCase):
    def test_everything_maps_to_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = SharedWorkspaceManager(tmp)
            ws = manager.create("task_x")
            self.assertEqual(Path(ws.path), Path(tmp).resolve())
            self.assertEqual(ws.isolation_type, "shared")
            manager.cleanup("task_x")     # no-op, must not raise


if __name__ == "__main__":
    unittest.main()
