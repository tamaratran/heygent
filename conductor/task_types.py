"""The Task: the user-facing unit of work.

Users interact with tasks, never with raw provider sessions. Task IDs are
stable and owned by this application; provider session IDs are implementation
details attached to tasks.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass, field

from .storage import now_iso

TASK_STATUSES = ("queued", "starting", "running", "waiting_for_user",
                 "paused", "interrupted", "completed", "failed", "cancelled")

# The CLIs with an adapter in code. Not the only ones a task may name: a
# CLI described in providers.json (configured_adapter) is a provider too,
# and a task stored under one must still load after the entry is gone.
PROVIDERS = ("claude-code", "codex", "gemini", "cursor")
PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")

ISOLATION_TYPES = ("git-worktree", "provider-managed", "shared", "sandbox")

# Where the human sees the worker. Task identity never depends on it.
SURFACE_MODES = ("visible", "background", "notify-only")


def new_task_id() -> str:
    return "task_" + secrets.token_hex(4)


@dataclass
class Workspace:
    path: str
    branch: str | None = None
    isolation_type: str = "shared"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Workspace":
        return cls(path=data["path"], branch=data.get("branch"),
                   isolation_type=data.get("isolation_type", "shared"))


@dataclass
class Task:
    id: str
    title: str
    goal: str
    project_id: str = ""          # every task belongs to exactly one project
    status: str = "queued"
    provider: str = "claude-code"
    provider_session_id: str | None = None
    workspace: Workspace | None = None
    surface_mode: str = "visible"
    surface: dict | None = None    # SurfaceHandle.to_dict(), if one exists
    computer: bool = False         # the user allowed this task to drive the GUI
    result: dict | None = None     # SubagentResult.to_dict(), when finished
    context_path: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    # When the user last addressed this task. updated_at moves on any
    # bookkeeping write - a surface change, a reconcile, a recovery sweep -
    # so it cannot tell a live task from one nothing has touched in an hour.
    last_instruction_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.status not in TASK_STATUSES:
            raise ValueError(f"unknown task status: {self.status!r}")
        if self.provider not in PROVIDERS and \
                not PROVIDER_NAME.match(str(self.provider)):
            raise ValueError(f"unknown provider: {self.provider!r}")
        if self.surface_mode not in SURFACE_MODES:
            raise ValueError(f"unknown surface mode: {self.surface_mode!r}")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["workspace"] = self.workspace.to_dict() if self.workspace else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        workspace = data.get("workspace")
        return cls(
            id=data["id"], title=data["title"], goal=data["goal"],
            project_id=data.get("project_id", ""),
            status=data.get("status", "queued"),
            provider=data.get("provider", "claude-code"),
            provider_session_id=data.get("provider_session_id"),
            workspace=Workspace.from_dict(workspace) if workspace else None,
            surface_mode=data.get("surface_mode", "visible"),
            surface=data.get("surface"),
            computer=data.get("computer", False),
            result=data.get("result"),
            context_path=data.get("context_path", ""),
            created_at=data.get("created_at", now_iso()),
            updated_at=data.get("updated_at", now_iso()),
            last_instruction_at=data.get("last_instruction_at")
            or data.get("created_at") or now_iso(),
        )
