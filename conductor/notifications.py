"""Session-aware notifications: one canonical object, many surfaces.

Every meaningful worker update carries one immutable task identity and fans
out into the transient popup, the persistent activity dropdown, voice
awareness, and observability - no surface ever infers which session an
update belongs to (the spec's required invariant).

    AgentEvent -> Conductor bus event -> NotificationService
        -> TaskNotification -> store (dropdown)  +  popup callback

The store is a lightweight local history under the conductor home. It is
not canonical task state - the dropdown reads notifications, task status
comes from state.json, and both surfaces therefore always agree with what
the voice Manager sees.
"""

from __future__ import annotations

import hashlib

import secrets
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .observability import ObservabilityEvent, application_log
from .plain_text import plain_text
from .storage import atomic_write_json, now_iso, read_json

NOTIFICATION_TYPES = ("progress", "milestone", "needs_input", "completed",
                      "failed", "warning", "info")

# Popup-worthy (spec section 7); everything else is dropdown-only.
POPUP_TYPES = ("milestone", "needs_input", "completed", "failed", "warning")

_MAX_STORED = 500


@dataclass
class TaskNotification:
    project_id: str
    task_id: str
    type: str
    title: str
    body: str = ""
    project_name: str = ""
    task_title: str = ""
    # Stable identity: a completion notification is tied to the exact
    # subagent and provider session that produced the result.
    subagent_id: str = ""
    provider_session_id: str = ""
    dedupe_key: str = ""          # replay-safe: same key -> one notification
    id: str = field(default_factory=lambda: "notif_" + secrets.token_hex(5))
    created_at: str = field(default_factory=now_iso)
    seq: int = 0                  # store-assigned; breaks same-second ties
    read: bool = False
    source_event_id: str = ""

    def __post_init__(self) -> None:
        if self.type not in NOTIFICATION_TYPES:
            raise ValueError(f"unknown notification type: {self.type!r}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskNotification":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__
                      if k in data})


class NotificationStore:
    """Persistent history at <home>/notifications.json; survives restarts so
    "needs your input" is never lost because the app was closed."""

    def __init__(self, home: str | Path) -> None:
        self.path = Path(home) / "notifications.json"
        self._lock = threading.Lock()
        data = read_json(self.path) or {"notifications": []}
        self._items = [TaskNotification.from_dict(item)
                       for item in data["notifications"]]
        self._seq = max((n.seq for n in self._items), default=0)

    def _persist(self) -> None:
        self._items = self._items[-_MAX_STORED:]
        atomic_write_json(self.path, {
            "notifications": [n.to_dict() for n in self._items]})

    def add(self, notification: TaskNotification) -> TaskNotification:
        """Append, coalescing chatter: an unread *progress* update replaces
        the previous unread progress update for the same task, while
        semantic transitions (milestone, failed, needs_input...) always
        remain separate entries (spec section 22)."""
        with self._lock:
            # Idempotence: completion (and other keyed) events may be
            # replayed by reconciliation; the same dedupe key never
            # produces a second user notification.
            if notification.dedupe_key:
                for existing in self._items:
                    if existing.dedupe_key == notification.dedupe_key:
                        return existing
            self._seq += 1
            notification.seq = self._seq
            if notification.type == "progress":
                for i in range(len(self._items) - 1, -1, -1):
                    existing = self._items[i]
                    if existing.task_id != notification.task_id:
                        continue
                    if existing.type == "progress" and not existing.read:
                        del self._items[i]
                    break              # only look at the task's latest entry
            self._items.append(notification)
            self._persist()
        return notification

    def list(self, project_id: str | None = None,
             task_id: str | None = None,
             unread_only: bool = False) -> list[TaskNotification]:
        with self._lock:
            items = list(self._items)
        if project_id:
            items = [n for n in items if n.project_id == project_id]
        if task_id:
            items = [n for n in items if n.task_id == task_id]
        if unread_only:
            items = [n for n in items if not n.read]
        return items

    def unread_count(self) -> int:
        return len(self.list(unread_only=True))

    def update_body(self, notification_id: str, body: str) -> None:
        """A later turn on the same instruction: keep the item, carry the
        newer text. Identity, order and read state are untouched."""
        with self._lock:
            for item in self._items:
                if item.id == notification_id:
                    item.body = body[:500]
            self._persist()

    def mark_read(self, notification_id: str) -> None:
        with self._lock:
            for item in self._items:
                if item.id == notification_id:
                    item.read = True
            self._persist()

    def mark_task_read(self, task_id: str) -> None:
        with self._lock:
            for item in self._items:
                if item.task_id == task_id:
                    item.read = True
            self._persist()

    def groups(self) -> list[dict]:
        """Notifications grouped by immutable task id (never by title -
        two projects may both have "Fix login"), newest activity first,
        newest notification first within a group."""
        by_task: dict[str, list[TaskNotification]] = {}
        for item in self.list():
            by_task.setdefault(item.task_id, []).append(item)
        groups = []
        for task_id, items in by_task.items():
            items.sort(key=lambda n: (n.created_at, n.seq), reverse=True)
            latest = items[0]
            groups.append({
                "project_id": latest.project_id,
                "task_id": task_id,
                "project_name": latest.project_name,
                "task_title": latest.task_title,
                "unread": sum(not n.read for n in items),
                "latest_at": latest.created_at,
                "notifications": [n.to_dict() for n in items],
            })
        groups.sort(key=lambda g: g["latest_at"], reverse=True)
        return groups


# Bus event type -> (notification type, title builder)
_EVENT_MAP = {
    "task.completed": ("completed", "Task completed"),
    "task.needs_input": ("needs_input", "Needs your input"),
    "task.approval_required": ("needs_input", "Needs your approval"),
    "task.failed": ("failed", "Task failed"),
    "task.context_updated": ("progress", "Working"),
    "task.progress": ("progress", "Working"),
    "task.created": ("info", "Working"),
    "task.paused": ("info", "Paused"),
    "task.interrupted": ("info", "Interrupted"),
    "task.resumed": ("info", "Resumed"),
    "task.cancelled": ("info", "Cancelled"),
    "task.handoff": ("info", "Received context from another task"),
}


@dataclass
class NotificationPolicy:
    """OS popups are selective interruptions; telemetry stays telemetry.

    "activity-only" updates the Activity Center row and history; only
    "activity+os" additionally interrupts the user. Routine progress, turn
    ends and lifecycle bookkeeping never pop by default.
    """
    progress: str = "activity-only"
    milestone: str = "activity-only"
    info: str = "activity-only"
    needs_input: str = "activity+os"
    completed: str = "activity+os"
    failed: str = "activity+os"
    warning: str = "activity+os"

    # How talkative the agents are allowed to be. Speech is the scarce
    # channel: a card costs a glance, a sentence costs the user's attention
    # and cannot be skimmed.
    SPOKEN_BY_MODE = {
        "silent": (),
        # A worker finishing is news, not chatter - it is the thing you
        # most want to hear when you are not looking at the screen, and
        # rationing it away took the point out of a voice agent. "Five
        # agents narrating themselves" is progress chatter, which is what
        # conversational adds.
        "important": ("needs_input", "failed", "warning", "completed"),
        "conversational": ("needs_input", "failed", "warning", "completed",
                           "progress", "milestone"),
    }

    def presents(self, notif_type: str) -> bool:
        return getattr(self, notif_type, "activity-only") == "activity+os"

    def speaks(self, notif_type: str, mode: str = "important") -> bool:
        """Whether this one is worth saying out loud.

        Separate from presents(): a notification can raise a card without
        being spoken. An agent opening a file is neither; an agent finishing
        is a card and, if the user wants it, a sentence; an agent blocked on
        a decision is always both.
        """
        allowed = self.SPOKEN_BY_MODE.get(mode,
                                          self.SPOKEN_BY_MODE["important"])
        return notif_type in allowed


def concise(text: str, limit: int = 140) -> str:
    """OS notification bodies are semantic one-liners; the full text lives
    in the Activity Center and the worker session."""
    text = " ".join(plain_text(str(text)).split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0] + "…"


class NotificationService:
    """Subscribes to the observability bus and turns task lifecycle events
    into notifications, resolving names from canonical state."""

    def __init__(self, conductor, store: NotificationStore | None = None,
                 on_activity=None,
                 on_notify: Callable[[TaskNotification], None] | None = None,
                 on_resolve: Callable[[str], None] | None = None,
                 policy: NotificationPolicy | None = None,
                 on_supersede: Callable[[TaskNotification], None] | None = None,
                 ) -> None:
        self.conductor = conductor
        # A notification's text moved on before it was read: the card
        # follows, and whatever was queued to say about it is replaced.
        # Never a new popup - that is what on_notify is for.
        self.on_supersede = on_supersede
        self.store = store or NotificationStore(conductor.projects.home)
        self.on_notify = on_notify        # called ONLY for os-worthy items
        self.on_resolve = on_resolve      # attention item no longer applies
        self.policy = policy or NotificationPolicy()
        # Card updates and interruptions are different things. on_activity
        # keeps the session's row current - it appears when the worker starts
        # and follows what it is doing - while on_notify is reserved for the
        # moments worth breaking into: an approval, a failure, a finish.
        self.on_activity = on_activity
        self._unsubscribe = conductor.bus.subscribe(self.handle_event)
        # Tasks with an instruction the worker has not yet answered.
        self._awaiting: set[str] = set()

    def close(self) -> None:
        self._unsubscribe()

    def _names(self, task_id: str) -> tuple[str, str, str]:
        try:
            _, task = self.conductor._find_task(task_id)
            project = self.conductor.projects.get(task.project_id)
            return (task.project_id,
                    project.display_name if project else "",
                    task.title)
        except KeyError:
            return "", "", ""

    def handle_event(self, event: ObservabilityEvent) -> None:
        if event.type in ("task.approval_resolved", "approval.resolved"):
            # The gate cleared - whichever side answered it. The attention
            # item resolves; a stale "needs approval" never lingers.
            self._resolve_attention(
                event.task_id,
                f"approval:{event.task_id}:"
                f"{event.data.get('approval_id', '')}")
            return
        # A completion is worth telling the user about when it answers
        # something they sent; a turn end with no instruction behind it is
        # the worker talking to itself. Tracked before the map lookup
        # because task.message_sent is not itself a notification.
        if event.task_id and event.type in ("task.created",
                                            "task.message_sent"):
            self._awaiting.add(event.task_id)
        # An instruction typed straight into the worker's window counts
        # too. task.message_sent only fires for sends the conductor made,
        # but the whole point of a PTY-hosted worker is that the user can
        # type into it - and when they did, the answer to what they asked
        # was treated as the worker talking to itself and swallowed by the
        # completion dedupe. The card updated in place (on_activity always
        # runs) while no notification, bell or spoken line ever arrived,
        # which reads exactly as "it is stuck on the old one".
        if event.task_id and event.type == "runtime.progress" \
                and event.data.get("source") == "user_message":
            self._awaiting.add(event.task_id)
        mapping = _EVENT_MAP.get(event.type)
        if mapping is None or not event.task_id:
            return
        notif_type, title = mapping
        body = (event.data.get("question") or event.data.get("summary")
                or event.data.get("error") or event.data.get("title") or "")
        body = str(body)[:300]
        dedupe_key = ""
        if event.type == "task.completed":
            # One notification per completion, not per task.
            #
            # A PTY worker stays alive and ends a turn for every instruction
            # it is given, so each turn end is a distinct answer the user
            # asked for. Keying only on the task meant the first turn
            # notified and every later one was swallowed: a worker would
            # report that it was oriented, then finish the actual work in
            # silence. The summary is part of the key so a replayed or
            # reconciled completion still deduplicates.
            title = "✓ Completed" if event.data.get("success", True) \
                else "Finished with problems"
            warnings = event.data.get("warnings") or []
            if warnings:
                body += "\n\nWarning:\n" + "\n".join(
                    f"- {w}" for w in warnings[:4])
            # Keyed on the instruction this completion answers, so the next
            # thing the user asks for gets its own notification while a
            # replay collapses into the previous one.
            answered = event.task_id in self._awaiting
            self._awaiting.discard(event.task_id)
            prior = [n for n in self.store.list(task_id=event.task_id)
                     if n.type == "completed" and n.dedupe_key]
            if answered or not prior:
                # Count from the durable store, not memory: a restarted
                # service must land on the same key as the one that stored
                # it, or reconciliation would re-notify every launch.
                dedupe_key = f"completed:{event.task_id}:{len(prior) + 1}"
            else:
                dedupe_key = prior[-1].dedupe_key
        elif event.type == "task.approval_required":
            # Approval identity is stable: reconciliation re-observing the
            # same approval never creates another item or popup.
            approval_id = (event.data.get("approval") or {}).get(
                "approval_id", "")
            title = "Approval needed"
            dedupe_key = f"approval:{event.task_id}:{approval_id}"
        elif event.type == "task.needs_input":
            title = "Needs your answer"
            dedupe_key = f"input:{event.task_id}:{body[:80]}"
        elif event.type == "task.failed":
            dedupe_key = f"failed:{event.task_id}"
        project_id, project_name, task_title = self._names(event.task_id)
        notification = TaskNotification(
            project_id=project_id or (event.project_id or ""),
            task_id=event.task_id, type=notif_type, title=title,
            body=plain_text(body)[:500], project_name=project_name,
            task_title=task_title, source_event_id=event.event_id,
            subagent_id=f"sub_{event.task_id}",
            provider_session_id=event.provider_session_id or "",
            dedupe_key=dedupe_key)
        stored = self.store.add(notification)
        if stored is not notification and notif_type == "completed" \
                and not stored.read and stored.body != notification.body:
            # The same instruction, a later turn: the worker ends one turn
            # to say what it will do and another with what it found. One
            # notification for both is right - but it has to carry the
            # LATER one. It carried the first: the user was read "I'll pull
            # PR #29 and read its diff" and never "PR #29 exists: ...".
            # Latest wins while it is still unread; once read, later turns
            # are the chatter the dedupe exists to keep quiet.
            self.store.update_body(stored.id, notification.body)
            if self.on_supersede:
                try:
                    self.on_supersede(stored)
                except Exception:
                    application_log("ui", "notification.supersede_callback_failed",
                                    "the supersede callback failed",
                                    severity="error", exc_info=True,
                                    task_id=stored.task_id)
        if self.on_activity:
            # Always: the row is a live view of the session, not a log of
            # interruptions, so it updates even for deduplicated events.
            try:
                self.on_activity(stored)
            except Exception:
                application_log("ui", "notification.activity_callback_failed",
                                "the activity callback failed",
                                severity="error", exc_info=True,
                                task_id=stored.task_id,
                                notification_type=stored.type)
        if stored is not notification:
            return                     # deduplicated: already surfaced
        if self.on_notify and self.policy.presents(notif_type):
            try:
                self.on_notify(notification)
            except Exception:
                # A UI hiccup must not break the bus, but it must be visible.
                application_log("ui", "notification.notify_callback_failed",
                                "the notify callback failed",
                                severity="error", exc_info=True,
                                task_id=notification.task_id,
                                notification_type=notification.type)

    def _resolve_attention(self, task_id: str | None, dedupe_key: str) -> None:
        for item in self.store.list(task_id=task_id):
            if item.dedupe_key == dedupe_key and not item.read:
                self.store.mark_read(item.id)
                if self.on_resolve:
                    try:
                        self.on_resolve(item.id)
                    except Exception:
                        application_log(
                            "ui", "notification.resolve_callback_failed",
                            "the resolve callback failed", severity="error",
                            exc_info=True, task_id=item.task_id,
                            notification_id=item.id)

    def snapshot(self) -> dict:
        """The Activity Center: one row per Managed Subagent, keyed by task
        id, mutating in place - never an event pile. Sections derive from
        lifecycle state; the Manager and every surface read the same
        canonical truth, so they can never disagree (spec sections 12, 20,
        22). Event history stays underneath each row, not on top of the
        screen."""
        activity = getattr(self.conductor, "_activity", {})
        started = getattr(self.conductor, "started_at", "")
        history = {g["task_id"]: g for g in self.store.groups()}
        needs, active, paused, recent = [], [], [], []
        for task in self.conductor.list_tasks():
            group = history.get(task.id, {})
            items = group.get("notifications", [])
            open_attention = [n for n in items
                              if n["type"] == "needs_input"
                              and not n["read"]]
            project = self.conductor.projects.get(task.project_id)
            row = {
                "task_id": task.id,
                "subagent_id": f"sub_{task.id}",
                "project_id": task.project_id,
                "project_name": project.display_name if project else "",
                "task_title": task.title,
                "status": task.status,
                "activity": (open_attention[0]["body"][:100]
                             if open_attention
                             else activity.get(task.id, "")[:100]),
                "unread": group.get("unread", 0),
                "latest_at": group.get("latest_at", task.updated_at),
                "notifications": items[:8],     # history, on expand only
            }
            if open_attention:
                row["status"] = "waiting_for_approval" if "approval" in \
                    open_attention[0].get("dedupe_key", "") else \
                    "waiting_for_input"
                needs.append(row)
            elif task.status in ("starting", "running"):
                active.append(row)
            elif task.status in ("paused", "interrupted"):
                paused.append(row)
            elif task.updated_at >= started:
                # Finished during this run. Measured: RECENT was the last
                # eight finishes of all time - five days of sessions the
                # user had long moved on from, on every launch.
                recent.append(row)
        for bucket in (needs, active, paused, recent):
            bucket.sort(key=lambda r: r["latest_at"], reverse=True)
        return {
            "unread": self.store.unread_count(),
            "sections": [
                {"name": "NEEDS ATTENTION", "groups": needs},
                {"name": "ACTIVE", "groups": active},
                {"name": "PAUSED", "groups": paused},
                {"name": "RECENT", "groups": recent[:8]},
            ],
        }
