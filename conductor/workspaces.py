"""Workspace isolation: each write-capable task gets its own directory.

Two concurrent workers editing the same checkout would trample each other, so
independent tasks get git worktrees - same repository, separate working
directories, separate branches. Nothing above this module knows how a
worktree is made (Invariant 7's cousin: don't couple the architecture to the
isolation mechanism).
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from .task_types import Workspace


class WorkspaceManager(ABC):
    @abstractmethod
    def create(self, task_id: str) -> Workspace: ...

    @abstractmethod
    def get(self, task_id: str) -> Workspace: ...

    @abstractmethod
    def cleanup(self, task_id: str, force: bool = False) -> None: ...


class SharedWorkspaceManager(WorkspaceManager):
    """No isolation: every task works in the project root.

    Fine for read-only tasks and for projects that are not git repositories.
    Write-capable concurrent tasks should not use this unless explicitly
    configured (Invariant 5).
    """

    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root).resolve()

    def create(self, task_id: str) -> Workspace:
        return Workspace(path=str(self.root), isolation_type="shared")

    def get(self, task_id: str) -> Workspace:
        return Workspace(path=str(self.root), isolation_type="shared")

    def cleanup(self, task_id: str, force: bool = False) -> None:
        pass                         # nothing was created, nothing to remove


class GitWorktreeManager(WorkspaceManager):
    """One git worktree and one branch per task.

    Worktrees live in a sibling directory of the repo -
    `<repo>-worktrees/<task_id>` - so a worker inside its workspace can never
    wander into another task's files by relative path.

    cleanup() removes the worktree directory but keeps the branch: the
    branch is the task's work, and deleting it is a user decision
    (merge_task / discard_task come later in the spec). The Conductor
    calls it when a task ends - completed, cancelled, or failed before a
    worker ever ran - and never with force: a directory holding
    uncommitted work is refused, reported, and left for the user.
    """

    def __init__(self, repo_root: str | Path,
                 worktrees_dir: str | Path | None = None,
                 branch_prefix: str = "agent/") -> None:
        self.repo = Path(repo_root).resolve()
        if not (self.repo / ".git").exists():
            raise ValueError(f"{self.repo} is not a git repository; "
                             "use SharedWorkspaceManager instead")
        self.worktrees = (Path(worktrees_dir).resolve() if worktrees_dir
                          else self.repo.parent / f"{self.repo.name}-worktrees")
        self.branch_prefix = branch_prefix

    def _git(self, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(self.repo), *args],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: "
                               f"{result.stderr.strip() or result.stdout.strip()}")
        return result.stdout

    def _path(self, task_id: str) -> Path:
        return self.worktrees / task_id

    def _branch(self, task_id: str) -> str:
        return f"{self.branch_prefix}{task_id}"

    def create(self, task_id: str) -> Workspace:
        path, branch = self._path(task_id), self._branch(task_id)
        if path.exists():
            return self.get(task_id)     # idempotent: recovery may re-ask
        self.worktrees.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "-b", branch, str(path))
        return Workspace(path=str(path), branch=branch,
                         isolation_type="git-worktree")

    def get(self, task_id: str) -> Workspace:
        path = self._path(task_id)
        if not path.exists():
            raise KeyError(f"no workspace for {task_id}")
        return Workspace(path=str(path), branch=self._branch(task_id),
                         isolation_type="git-worktree")

    def cleanup(self, task_id: str, force: bool = False) -> None:
        """Remove the worktree directory. Refuses if it has uncommitted
        changes unless force=True; the branch always survives."""
        path = self._path(task_id)
        if not path.exists():
            return
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        self._git(*args, str(path))
