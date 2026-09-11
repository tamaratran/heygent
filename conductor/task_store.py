"""The task registry: state.json under .myconductor/.

Only this process mutates canonical state, guarded by one in-process lock and
written atomically. Workers report events to the Conductor; they never touch
state.json themselves.

Layout on disk:

    .myconductor/
      state.json
      project.md
      tasks/
        task_ab12cd34/
          context.md
          events.jsonl
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .observability import ObservabilityBus, ObservabilityEvent
from .storage import atomic_write_json, now_iso
from .task_types import Task, Workspace, new_task_id

STATE_DIR = ".myconductor"
STATE_VERSION = 1


class CorruptStateError(RuntimeError):
    """state.json exists but is not valid JSON.

    Refusing to load beats silently starting fresh, which would overwrite
    the user's last recoverable state on the next mutation.
    """


class TaskStore:
    def __init__(self, project_root: str | Path,
                 bus: ObservabilityBus | None = None,
                 state_dir: str | Path | None = None,
                 project_id: str = "") -> None:
        """state_dir relocates the store (v2 keeps it under
        ~/.voice-conductor/projects/<id>/ instead of inside the repo);
        project_id stamps every created task with its owning project."""
        self.root = Path(project_root).resolve()
        self.dir = Path(state_dir).resolve() if state_dir \
            else self.root / STATE_DIR
        self.project_id = project_id
        self.state_path = self.dir / "state.json"
        self.bus = bus or ObservabilityBus()
        self._lock = threading.Lock()
        if self.state_path.exists():
            try:
                self._state = json.loads(self.state_path.read_text())
            except json.JSONDecodeError as exc:
                raise CorruptStateError(
                    f"{self.state_path} is corrupt; refusing to overwrite. "
                    "Inspect or remove it by hand.") from exc
        else:
            self._state = {
                "version": STATE_VERSION,
                "project": {"root": str(self.root)},
                "manager": {"provider": None, "session_id": None},
                "tasks": {},
            }
            # Persist immediately so a fresh project has recoverable state
            # from the first moment, not from the first mutation.
            self._persist()

    def _persist(self) -> None:
        atomic_write_json(self.state_path, self._state)
        self.bus.emit(ObservabilityEvent(type="storage.state_written",
                                         component="storage"))

    # -- paths ------------------------------------------------------------
    def task_dir(self, task_id: str) -> Path:
        return self.dir / "tasks" / task_id

    def context_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "context.md"

    def events_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "events.jsonl"

    def project_md_path(self) -> Path:
        return self.dir / "project.md"

    # -- tasks --------------------------------------------------------------
    def create(self, title: str, goal: str, provider: str = "claude-code",
               task_id: str | None = None, computer: bool = False) -> Task:
        """task_id is generated unless given - evals and tests seed known ids
        so cases can say expected: task_login."""
        if task_id is not None and self.get(task_id) is not None:
            raise ValueError(f"task id already exists: {task_id}")
        task = Task(id=task_id or new_task_id(), title=title, goal=goal,
                    project_id=self.project_id, status="queued",
                    provider=provider, computer=computer)
        task.context_path = f"tasks/{task.id}/context.md"
        with self._lock:
            self._state["tasks"][task.id] = task.to_dict()
            self._persist()
        self.task_dir(task.id).mkdir(parents=True, exist_ok=True)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            data = self._state["tasks"].get(task_id)
        return Task.from_dict(data) if data else None

    def list(self) -> list[Task]:
        with self._lock:
            items = list(self._state["tasks"].values())
        return [Task.from_dict(item) for item in items]

    def update(self, task_id: str, **fields) -> Task:
        """Change a task and persist in one atomic step.

        Accepts any Task field, e.g. status="running",
        provider_session_id="...", workspace=Workspace(...).
        """
        with self._lock:
            data = self._state["tasks"].get(task_id)
            if data is None:
                raise KeyError(f"no such task: {task_id}")
            task = Task.from_dict(data)
            for key, value in fields.items():
                if not hasattr(task, key):
                    raise AttributeError(f"Task has no field {key!r}")
                setattr(task, key, value)
            task.updated_at = now_iso()
            # Round-trip through the constructor so validation still applies.
            task = Task.from_dict(task.to_dict())
            self._state["tasks"][task_id] = task.to_dict()
            self._persist()
            previous = data.get("status")
        if previous != task.status:
            # The one seam every lifecycle change passes - pause, resume,
            # cancel, close, interrupt, recover, all nineteen call sites -
            # so canonical subagent state follows the Task without each
            # caller remembering to say so.
            self.bus.emit(ObservabilityEvent(
                type="task.status_changed", component="storage",
                project_id=self.project_id, task_id=task_id,
                data={"from": previous, "to": task.status}))
        return task

    # -- manager ------------------------------------------------------------
    def set_manager(self, provider: str, session_id: str | None) -> None:
        with self._lock:
            self._state["manager"] = {"provider": provider,
                                      "session_id": session_id}
            self._persist()

    def manager(self) -> dict:
        with self._lock:
            return dict(self._state["manager"])

    # -- recovery -------------------------------------------------------------
    def running_tasks(self) -> list[Task]:
        """Tasks that claim to be in flight; startup checks these against the
        provider to decide reconnect vs mark-interrupted."""
        return [task for task in self.list()
                if task.status in ("starting", "running", "waiting_for_user")]
