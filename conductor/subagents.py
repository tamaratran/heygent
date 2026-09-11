"""Managed subagents: the coding workers as persistent, supervised children
of the Main Manager.

Not provider-internal helpers - first-class, user-addressable workers the
Manager spawns once, stays subscribed to, messages again, answers approvals
for, recovers in place, and receives a structured result from. The Task is
the durable user goal; the ManagedSubagent is the live worker executing it.

This module is a *view*, not a second store: everything derives from
canonical task state plus runtime health, so it can never disagree with what
notifications, voice, or the dropdown report. The Manager tool surface maps
onto the subagent operations one to one:

    spawn_subagent          create_task
    message_subagent        send_to_task
    inspect_subagent        inspect_task
    interrupt_subagent      interrupt_task
    resume_subagent         resume_task
    approve_subagent_action approve_task_action
    deny_subagent_action    deny_task_action
    focus_subagent          focus_task
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .task_types import Task

# The full supervision vocabulary. These are different states requiring
# different actions and must never be collapsed: waiting on approval is not
# stuck, paused is not dead, interrupted is not failed, and recovering is
# an explicit in-progress reconciliation.
SUBAGENT_STATUSES = ("starting", "working", "idle", "waiting_for_input",
                     "waiting_for_approval", "paused", "interrupted",
                     "recovering", "completed", "failed", "cancelled")


@dataclass
class SubagentResult:
    """The structured outcome the Manager receives when a worker finishes.
    Built from the worker's own events (final reply + report_progress
    checkpoints), persisted on the task - never reconstructed by rereading
    a transcript later."""
    summary: str
    success: bool
    files_changed: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SubagentResult":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__
                      if k in data})


@dataclass
class ManagedSubagent:
    """One live (or finished) worker: task identity, provider session,
    supervision status, and result."""
    id: str                       # stable; derived from the task id
    task_id: str
    project_id: str
    provider: str
    provider_session_id: str | None
    status: str
    title: str
    result: SubagentResult | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["result"] = self.result.to_dict() if self.result else None
        return data


def subagent_status(task: Task, provider_health: str | None,
                    has_pending_approval: bool,
                    recovering: bool = False) -> str:
    """Canonical task state + provider health -> supervision vocabulary.
    Waiting on an approval is never 'stuck' - it is a state the Manager
    handles - and an in-flight recovery is visible, not silent."""
    if recovering:
        return "recovering"
    if task.status in ("completed", "failed", "cancelled", "interrupted",
                       "paused"):
        return task.status
    if has_pending_approval or provider_health == "waiting_for_approval":
        return "waiting_for_approval"
    if task.status == "waiting_for_user":
        return "waiting_for_input"
    if task.status in ("queued", "starting"):
        return "starting"
    if provider_health == "idle":
        return "idle"       # session alive and available, no turn running
    return "working"


def build_subagent(task: Task, provider_health: str | None = None,
                   has_pending_approval: bool = False,
                   recovering: bool = False) -> ManagedSubagent:
    result = SubagentResult.from_dict(task.result) if task.result else None
    return ManagedSubagent(
        id=f"sub_{task.id}", task_id=task.id, project_id=task.project_id,
        provider=task.provider,
        provider_session_id=task.provider_session_id,
        status=subagent_status(task, provider_health, has_pending_approval,
                               recovering),
        title=task.title, result=result)
