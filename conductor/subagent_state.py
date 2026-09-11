"""Canonical Managed Subagent state, and the one reducer that changes it.

The objective branch of the design:

    provider event -> AgentEvent -> reduce() -> SubagentState -> card

Nothing on this branch consults a model, a notification record, or the
voice. The card is a projection of SubagentState; SubagentState is the
result of applying events in order. Everything that used to answer "what
is this worker doing?" by reading a notification, a transcript line, or
the Boss's last sentence reads this instead.

Three guards live here and nowhere else:

    duplicate   the same event_id applied twice changes nothing;
    stale       a lower sequence than one already applied changes nothing,
                so a late "running tests" cannot pull a finished worker
                back to Working;
    terminal    completed / failed / cancelled are final for events. Only
                an explicit lifecycle decision (apply_lifecycle) moves a
                worker out of them.

One judgement is deliberate and worth stating. A PTY worker ends a TURN,
not a task: "completed" from the runtime means the worker answered and is
standing by, and the product has always treated marking a task done as
the Manager's or the user's call. The reducer keeps that: a runtime
completion makes the worker `idle` with a result attached, and the card
renders idle-with-result as "Completed - <summary>", which is what the
user sees as the tick today. `completed` as a status is reached through
apply_lifecycle, when the task itself is closed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .agent_activity import classify
from .agent_events import AgentEvent, SUMMARY_CEILING, keep_end
from .storage import atomic_write_json, now_iso, read_json
from .subagents import SUBAGENT_STATUSES

TERMINAL = ("completed", "failed", "cancelled")
SEEN_EVENTS_KEPT = 64          # enough to absorb a replay burst, bounded


@dataclass
class SubagentState:
    id: str                      # sub_<task_id>
    task_id: str
    project_id: str
    provider: str
    provider_session_id: str | None
    title: str
    status: str = "starting"
    activity: str = ""           # normalized AgentActivity
    activity_text: str = ""      # the detail after the label, if any
    pending_approval: dict | None = None
    pending_input: dict | None = None
    result: dict | None = None
    last_sequence: int = 0
    # The epoch last_sequence was counted in (AgentEvent.epoch). A new
    # epoch - a restarted runtime - starts the count again; comparing its
    # 1 against the old 45 dropped every event until the count caught up.
    last_epoch: str = ""
    seen_event_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    completed_at: str | None = None
    # The Boss session that started this worker. Explicit and persisted:
    # parentage is never inferred from timing, project, or a window.
    parent_boss_session_id: str | None = None
    # What the user has seen. `revision` counts applied changes to this
    # state; `result_revision` and `attention_revision` are the revisions
    # at which the latest result and the latest question arrived; and
    # `dismissed_at_revision` is where the user waved the card away.
    # A card is worth reopening only for a result or a question that
    # arrived AFTER that - "dismissed" means "seen up to here", not
    # "hidden until the next tick". Measured before this: a dismissed
    # card came back on every later change to an idle worker, and on
    # every restart, because nothing remembered the dismissal.
    revision: int = 0
    result_revision: int = 0
    attention_revision: int = 0
    dismissed_at_revision: int = 0

    def __post_init__(self) -> None:
        if self.status not in SUBAGENT_STATUSES:
            raise ValueError(f"unknown subagent status: {self.status!r}")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "task_id": self.task_id,
            "project_id": self.project_id, "provider": self.provider,
            "provider_session_id": self.provider_session_id,
            "title": self.title, "status": self.status,
            "activity": self.activity, "activity_text": self.activity_text,
            "pending_approval": self.pending_approval,
            "pending_input": self.pending_input, "result": self.result,
            "last_sequence": self.last_sequence,
            "last_epoch": self.last_epoch,
            "seen_event_ids": list(self.seen_event_ids),
            "created_at": self.created_at, "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "parent_boss_session_id": self.parent_boss_session_id,
            "revision": self.revision,
            "result_revision": self.result_revision,
            "attention_revision": self.attention_revision,
            "dismissed_at_revision": self.dismissed_at_revision,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SubagentState":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known["seen_event_ids"] = list(known.get("seen_event_ids") or [])
        return cls(**known)


@dataclass(frozen=True)
class Transition:
    """What one event did to the state - or why it did nothing."""
    outcome: str        # applied | duplicate | stale | terminal | noop
    before: str
    after: str
    event_id: str
    kind: str           # the event's type
    changed: tuple = ()  # which fields moved, for the log


def reduce(state: SubagentState, event: AgentEvent) -> tuple[SubagentState, Transition]:
    """Apply one normalized event. Pure: returns a new state."""
    before = state.status
    if event.event_id in state.seen_event_ids:
        return state, Transition("duplicate", before, before, event.event_id,
                                 event.type)
    if event.sequence and event.sequence < state.last_sequence \
            and _same_epoch(state, event):
        return state, Transition("stale", before, before, event.event_id,
                                 event.type)
    if state.status in TERMINAL:
        return _remember(state, event), Transition(
            "terminal", before, before, event.event_id, event.type)

    changed: list[str] = []
    new = replace(state)
    kind = event.type
    if kind == "started":
        if new.status in ("starting", "idle"):
            new.status = "working"
    elif kind == "progress":
        activity, detail = classify(event.summary, event.detail)
        if activity != "waiting":
            new.activity, new.activity_text = activity, detail
            changed.append("activity")
        if new.status in ("starting", "idle"):
            new.status = "working"
    elif kind == "checkpoint":
        new.activity, new.activity_text = "other", _short(event.summary)
        changed.append("activity")
        if new.status in ("starting", "idle"):
            new.status = "working"
    elif kind == "approval_required":
        approval = (event.detail or {}).get("approval") or {}
        new.status = "waiting_for_approval"
        new.pending_approval = {
            "question": event.question[:300],
            "approval_id": approval.get("approval_id", "")}
        changed.append("pending_approval")
    elif kind == "approval_resolved":
        if new.pending_approval is not None:
            new.pending_approval = None
            changed.append("pending_approval")
        if new.status == "waiting_for_approval":
            new.status = "working"
    elif kind == "needs_input":
        new.status = "waiting_for_input"
        new.pending_input = {"question": event.question[:300]}
        changed.append("pending_input")
    elif kind == "completed":
        # A turn ended: the worker answered and is standing by. See the
        # module docstring for why this is idle-with-result, not completed.
        new.status = "idle"
        new.result = {"summary": keep_end(event.summary, SUMMARY_CEILING)
                      or "Done.",
                      "success": True,
                      **{k: v for k, v in (event.detail or {}).items()
                         if k in ("files_changed", "findings",
                                  "next_steps", "warnings")}}
        new.pending_input = None
        new.activity, new.activity_text = "", ""
        changed.append("result")
    elif kind == "failed":
        new.status = "failed"
        new.result = {"summary": keep_end(event.error, SUMMARY_CEILING)
                      or "Failed.",
                      "success": False}
        new.completed_at = now_iso()
        changed.append("result")

    if new.status != before:
        changed.insert(0, "status")
    new = _remember(new, event)
    if not changed:
        return new, Transition("noop", before, before, event.event_id, kind)
    new.updated_at = now_iso()
    new.revision = state.revision + 1
    if "result" in changed:
        new.result_revision = new.revision
    if ("pending_approval" in changed and new.pending_approval) or \
            ("pending_input" in changed and new.pending_input):
        new.attention_revision = new.revision
    return new, Transition("applied", before, new.status, event.event_id,
                           kind, tuple(changed))


def dismiss(state: SubagentState) -> SubagentState:
    """The user waved this worker's card away: seen up to here. Pure.

    The dismissal is itself a revision, so it is never recorded as 0 -
    which is what "never dismissed" looks like (an old sidecar, a state
    built fresh from its Task). Measured 2026-08-29: every worker that
    predated the field sat at revision 0 for good once it was idle, its
    dismissal was written as 0, the projection read that as nothing,
    and the same three cards came back on each of three restarts.
    """
    new = replace(state)
    new.revision = state.revision + 1
    new.dismissed_at_revision = new.revision
    return new


def apply_lifecycle(state: SubagentState, status: str) -> SubagentState:
    """An explicit decision - pause, resume, cancel, close, interrupt,
    recover - made by the Conductor or the user, not observed from the
    worker. The only way out of a terminal status."""
    if status not in SUBAGENT_STATUSES:
        raise ValueError(f"unknown subagent status: {status!r}")
    new = replace(state, status=status, updated_at=now_iso())
    if status != state.status:
        # A change the user can see counts as a revision; it is not news
        # for a dismissed card (no result, no question) - a lifecycle
        # decision is the user's own, or the Conductor's.
        new.revision = state.revision + 1
    if status in TERMINAL:
        new.completed_at = new.completed_at or now_iso()
        new.pending_approval = None
        new.pending_input = None
        new.activity, new.activity_text = "", ""
    return new


def _same_epoch(state: SubagentState, event: AgentEvent) -> bool:
    """An unstamped event, or one from the epoch the state was last
    counted in, is compared against the high-water mark. Any other
    epoch is a restarted runtime counting from 1 again."""
    return not event.epoch or event.epoch == state.last_epoch


def _remember(state: SubagentState, event: AgentEvent) -> SubagentState:
    seen = (state.seen_event_ids + [event.event_id])[-SEEN_EVENTS_KEPT:]
    if _same_epoch(state, event):
        return replace(state, seen_event_ids=seen,
                       last_sequence=max(state.last_sequence, event.sequence))
    # A new epoch: its first event sets the mark, whatever it is.
    return replace(state, seen_event_ids=seen, last_epoch=event.epoch,
                   last_sequence=event.sequence)


def _short(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text[:limit - 3] + "…" if len(text) > limit else text


class SubagentStateStore:
    """Canonical state on disk, one file per subagent, written atomically.

    Not a second task store: the Task remains the durable user goal. This
    holds what the reducer needs that the Task does not carry - activity,
    pending decisions, applied sequence, seen event ids - and survives a
    restart so the card comes back showing the truth, not a replay.
    """

    def __init__(self, path_for: Callable[[str], Path]) -> None:
        self._path_for = path_for
        self._cache: dict[str, SubagentState] = {}

    def get(self, task_id: str) -> SubagentState | None:
        if task_id in self._cache:
            return self._cache[task_id]
        data = read_json(self._path_for(task_id), default=None)
        if not data:
            return None
        try:
            state = SubagentState.from_dict(data)
        except (TypeError, ValueError):
            return None
        self._cache[task_id] = state
        return state

    def save(self, state: SubagentState) -> None:
        self._cache[state.task_id] = state
        path = self._path_for(state.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, state.to_dict())

    def forget(self, task_id: str) -> None:
        self._cache.pop(task_id, None)
