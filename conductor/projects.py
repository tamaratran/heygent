"""Projects: first-class, globally registered, path-independent identities.

No project is assumed open when the user speaks. The registry lives under a
single conductor home (default ~/.voice-conductor/):

    <home>/
      global.json                  recent projects, focus, manager session
      projects/<projectId>/
        project.json               identity, path, fingerprint
        context.md                 durable project knowledge
        state.json                 that project's tasks (TaskStore)
        tasks/<taskId>/...

Project IDs are Conductor-owned and stable; the filesystem path may change
(Invariant 16: a moved repo does not destroy project identity).
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .storage import atomic_write_json, now_iso, read_json

PROJECT_STATUSES = ("available", "missing", "moved", "unavailable")

DEFAULT_HOME = Path.home() / ".voice-conductor"


def new_project_id() -> str:
    return "proj_" + secrets.token_hex(4)


@dataclass
class Project:
    id: str
    display_name: str
    root_path: str
    aliases: list[str] = field(default_factory=list)
    repo_root: str | None = None
    remote_url: str | None = None
    package_name: str | None = None
    status: str = "available"
    created_at: str = field(default_factory=now_iso)
    last_used_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.status not in PROJECT_STATUSES:
            raise ValueError(f"unknown project status: {self.status!r}")

    def names(self) -> list[str]:
        """Everything this project answers to, lowercased."""
        names = [self.display_name] + list(self.aliases)
        names.append(Path(self.root_path).name)
        if self.package_name:
            names.append(self.package_name)
        return [n.lower() for n in names if n]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__
                      if k in data})


class ProjectStore:
    """global.json plus one directory per project; single-writer, atomic."""

    def __init__(self, home: str | Path | None = None) -> None:
        self.home = Path(home).resolve() if home else DEFAULT_HOME
        self.global_path = self.home / "global.json"
        self._lock = threading.Lock()
        defaults = {
            "version": 1,
            "manager": {"provider": None, "session_id": None},
            "project_ids": [],
            "recent_project_ids": [],
            "focus": {"project_id": None, "task_id": None},
        }
        # Heal the file's shape key by key, not only when the whole file
        # is missing. Measured on 2026-09-01: a global.json holding
        # "focus": null made every manager tool die at set_focus with
        # 'NoneType' object does not support item assignment - and the
        # Boss, told create_task failed, started three workers for one
        # request. What is on disk is a snapshot, not a schema.
        on_disk = read_json(self.global_path) or {}
        self._global = dict(defaults)
        for key, value in on_disk.items():
            if value is not None:
                self._global[key] = value
        for key, fallback in defaults.items():
            if not isinstance(self._global.get(key), type(fallback)):
                self._global[key] = fallback if not isinstance(fallback, (
                    dict, list)) else type(fallback)(fallback)
        if not self.global_path.exists():
            atomic_write_json(self.global_path, self._global)

    # -- paths --------------------------------------------------------------
    def project_dir(self, project_id: str) -> Path:
        return self.home / "projects" / project_id

    def project_json(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def context_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "context.md"

    def workspaces_dir(self) -> Path:
        return self.home / "workspaces"

    def _persist_global(self) -> None:
        atomic_write_json(self.global_path, self._global)

    # -- projects -------------------------------------------------------------
    def register(self, display_name: str, root_path: str | Path,
                 aliases: list[str] | None = None,
                 repo_root: str | None = None,
                 remote_url: str | None = None,
                 package_name: str | None = None) -> Project:
        project = Project(id=new_project_id(), display_name=display_name,
                          root_path=str(Path(root_path).resolve()),
                          aliases=aliases or [], repo_root=repo_root,
                          remote_url=remote_url, package_name=package_name)
        with self._lock:
            atomic_write_json(self.project_json(project.id),
                              project.to_dict())
            self._global["project_ids"].append(project.id)
            self._touch_recent(project.id)
            self._persist_global()
        return project

    def get(self, project_id: str) -> Project | None:
        data = read_json(self.project_json(project_id))
        return Project.from_dict(data) if data else None

    def list(self) -> list[Project]:
        with self._lock:
            ids = list(self._global["project_ids"])
        return [p for p in (self.get(pid) for pid in ids) if p]

    def update(self, project_id: str, **fields) -> Project:
        with self._lock:
            project = self.get(project_id)
            if project is None:
                raise KeyError(f"no such project: {project_id}")
            for key, value in fields.items():
                if not hasattr(project, key):
                    raise AttributeError(f"Project has no field {key!r}")
                setattr(project, key, value)
            project.updated_at = now_iso()
            project = Project.from_dict(project.to_dict())   # re-validate
            atomic_write_json(self.project_json(project_id),
                              project.to_dict())
        return project

    def find_by_name(self, name: str) -> list[Project]:
        """Registered projects answering to this name, most recent first."""
        name = name.lower().strip()
        matches = [p for p in self.list()
                   if name in p.names()
                   or any(name in n for n in p.names())]
        recent = self.recent_ids()
        matches.sort(key=lambda p: recent.index(p.id)
                     if p.id in recent else len(recent))
        return matches

    # -- recency and focus ----------------------------------------------------
    def _touch_recent(self, project_id: str) -> None:
        recent = self._global["recent_project_ids"]
        if project_id in recent:
            recent.remove(project_id)
        recent.insert(0, project_id)
        del recent[10:]

    def touch(self, project_id: str) -> None:
        with self._lock:
            self._touch_recent(project_id)
            self._persist_global()
        self.update(project_id, last_used_at=now_iso())

    def recent_ids(self) -> list[str]:
        with self._lock:
            return list(self._global["recent_project_ids"])

    def focus(self) -> dict:
        with self._lock:
            return dict(self._global["focus"])

    def set_focus(self, project_id: str | None = None,
                  task_id: str | None = None) -> None:
        """Conversational focus is a hint, never authorization (spec 21)."""
        with self._lock:
            if project_id is not None:
                self._global["focus"]["project_id"] = project_id
            if task_id is not None:
                self._global["focus"]["task_id"] = task_id
            self._persist_global()

    # -- manager ------------------------------------------------------------
    def manager(self) -> dict:
        with self._lock:
            return dict(self._global["manager"])

    def set_manager(self, provider: str, session_id: str | None) -> None:
        with self._lock:
            self._global["manager"] = {"provider": provider,
                                       "session_id": session_id}
            self._persist_global()
