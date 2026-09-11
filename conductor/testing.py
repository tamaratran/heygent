"""Fake infrastructure: the complete Conductor is testable with no external
APIs - no LLM, no git, no subprocess.

Rule 80 of the spec: if reproducing a routing bug requires speaking into a
microphone while five real Claude sessions run, the architecture has failed.
These fakes are what keep every layer independently testable.
"""

from __future__ import annotations

from .agent_events import AgentEvent
from .runtime import CodingAgentRuntime, ExecutionTranscript, TaskExecution
from .task_types import Workspace
from .workspaces import WorkspaceManager


class FakeCodingAgentRuntime(CodingAgentRuntime):
    """Records every call and lets tests push AgentEvents by hand.

    Failure modes are switches, so tests can simulate a session that
    disappears (fail_resume), a provider that cannot start (fail_create),
    or a send into a dead session (fail_send).
    """

    def __init__(self, fail_create: bool = False, fail_send: bool = False,
                 fail_resume: bool = False,
                 transcript_dir: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.handlers: dict[str, list] = {}
        self.statuses: dict[str, str] = {}
        self.tasks: dict[str, str] = {}      # session -> task
        self.workspaces_used: dict[str, str] = {}
        self.pending: dict[str, dict] = {}   # session -> approvals
        self.fail_create = fail_create
        self.fail_send = fail_send
        self.fail_resume = fail_resume
        self.transcript = ExecutionTranscript(transcript_dir) \
            if transcript_dir else None
        self._counter = 0

    def _log(self, session_id: str, text: str) -> None:
        if self.transcript is not None and session_id in self.tasks:
            self.transcript.write(self.tasks[session_id], text)

    async def create_session(self, task_id, working_directory,
                             initial_prompt) -> str:
        if self.fail_create:
            raise RuntimeError("provider refused to start a session")
        # One task, one execution - same rule as the real runtime.
        for sid, owner in self.tasks.items():
            if owner == task_id and \
                    self.statuses.get(sid) != "disconnected":
                raise RuntimeError(f"task {task_id} already has a live "
                                   f"execution ({sid})")
        self._counter += 1
        session_id = f"sess_{self._counter}"
        self.calls.append(("create", task_id, working_directory,
                           initial_prompt))
        self.statuses[session_id] = "running"
        self.tasks[session_id] = task_id
        self.workspaces_used[session_id] = working_directory
        self.handlers[session_id] = []
        if self.transcript is not None:
            self.transcript.header(task_id, f"task {task_id}", session_id,
                                   working_directory)
            self.transcript.write(task_id, f"\n> {initial_prompt}\n")
        return session_id

    async def executions(self) -> list[TaskExecution]:
        return [TaskExecution(
                    task_id=self.tasks[sid], provider="claude-code",
                    provider_session_id=sid,
                    workspace_path=self.workspaces_used.get(sid, ""),
                    status=status,
                    transcript_path=str(self.transcript.path(self.tasks[sid]))
                    if self.transcript else None)
                for sid, status in self.statuses.items()
                if status != "disconnected" and sid in self.tasks]

    async def send(self, session_id, message) -> None:
        if self.fail_send or \
                self.statuses.get(session_id, "disconnected") == "disconnected":
            raise RuntimeError(f"cannot send to {session_id}")
        self.calls.append(("send", session_id, message))
        self._log(session_id, f"\n> {message}\n")
        self.statuses[session_id] = "running"

    async def interrupt(self, session_id) -> None:
        self.calls.append(("interrupt", session_id))
        self.statuses[session_id] = "idle"

    async def resume(self, session_id, working_directory=None) -> None:
        if self.fail_resume:
            raise RuntimeError("session is gone")
        self.calls.append(("resume", session_id, working_directory))
        self.statuses[session_id] = "idle"
        self.handlers.setdefault(session_id, [])

    async def get_status(self, session_id) -> str:
        return self.statuses.get(session_id, "disconnected")

    async def subscribe(self, session_id, handler):
        self.handlers.setdefault(session_id, []).append(handler)
        return lambda: self.handlers[session_id].remove(handler)

    async def destroy(self, session_id) -> None:
        self.calls.append(("destroy", session_id))
        self.statuses[session_id] = "disconnected"

    async def reconcile_session(self, session_id: str) -> str:
        if self.pending.get(session_id):
            return "waiting_for_approval"
        return await super().reconcile_session(session_id)

    async def pending_approvals(self, session_id: str) -> list[dict]:
        return [dict(approval)
                for approval in self.pending.get(session_id, {}).values()]

    async def resolve_approval(self, session_id: str, approval_id: str,
                               approve: bool) -> None:
        approvals = self.pending.get(session_id, {})
        if approval_id not in approvals:
            raise KeyError(f"approval {approval_id} is unknown or already "
                           "resolved")
        del approvals[approval_id]
        self.calls.append(("approve" if approve else "deny",
                           session_id, approval_id))
        self.statuses[session_id] = "running"

    # -- test controls ------------------------------------------------------
    def emit(self, session_id: str, event: AgentEvent) -> None:
        """Simulate the provider producing an event."""
        for handler in list(self.handlers.get(session_id, [])):
            handler(event)

    def emit_approval(self, session_id: str, approval_id: str,
                      description: str) -> dict:
        """Simulate the provider asking permission for something."""
        approval = {"approval_id": approval_id, "description": description}
        self.pending.setdefault(session_id, {})[approval_id] = approval
        self.emit(session_id, AgentEvent(type="approval_required",
                                         question=description,
                                         detail={"approval": approval}))
        return approval

    def vanish(self, session_id: str) -> None:
        """Simulate the provider session disappearing out from under us."""
        self.statuses[session_id] = "disconnected"


class FakeWorkspaceManager(WorkspaceManager):
    """Distinct fake paths per task, with failure switches.

    Paths are unique by construction so isolation invariants can be asserted
    without touching a real filesystem; real worktree behaviour is covered by
    the git integration tests.
    """

    def __init__(self, fail_create: bool = False,
                 fail_cleanup: bool = False) -> None:
        self.workspaces: dict[str, Workspace] = {}
        self.fail_create = fail_create
        self.fail_cleanup = fail_cleanup

    def create(self, task_id: str) -> Workspace:
        if self.fail_create:
            raise RuntimeError("workspace creation failed")
        if task_id not in self.workspaces:
            self.workspaces[task_id] = Workspace(
                path=f"/fake/worktrees/{task_id}",
                branch=f"agent/{task_id}", isolation_type="sandbox")
        return self.workspaces[task_id]

    def get(self, task_id: str) -> Workspace:
        if task_id not in self.workspaces:
            raise KeyError(f"no workspace for {task_id}")
        return self.workspaces[task_id]

    def cleanup(self, task_id: str, force: bool = False) -> None:
        if self.fail_cleanup:
            raise RuntimeError("cleanup failed")
        self.workspaces.pop(task_id, None)
