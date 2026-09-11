"""WorkspaceDecor: the cmux sidebar as a routing map.

The workspaces were correct and unreadable: every worker was a teal
`cond_task_xxxx`, the Boss another, and "where did the Boss route that
to?" had no visual answer. This subscriber turns the lifecycle events the
bus already carries into what a person can read at a glance:

- a worker's workspace is titled with its task's own words
  ("posely: Fix login flake"), not its bookkeeping name;
- its colour follows the task - teal while worked, orange when it waits
  on the user, green when done, red when failed;
- the workspace the Boss routes words to flashes in the sidebar, so a
  delegation that raises no window is still seen going somewhere;
- the delegation loop is written on both ends as a sidebar pill with the
  hour it happened: the worker carries "\u25c0 Boss 14:32" while the Boss's
  words are with it and "\u25b6 Boss 14:41" once it has reported back, and
  the Boss carries the mirror image naming the worker's task - so who is
  holding the ball, and since when, is readable without opening anything.

Identity is never in the title: lookups go by the description stamp
(cmux_runtime.managed_name), so dressing cannot lose a worker.

Dressing shells out to cmux, which is too slow for a bus handler on the
event loop; one worker thread drains a queue instead, and every job is
best effort - a sidebar that cannot be painted must not touch the task.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

from .observability import ObservabilityEvent, application_log
from .pty_manager import BOSS_TASK_ID
from .tmux_runtime import session_name

# What each lifecycle event says about the workspace's colour. Resolution
# events (the gate cleared, the worker resumed) return it to "working";
# terminal events park it.
_STATES = {
    "task.created": "working",
    "task.resumed": "working",
    "task.watch_resumed": "working",
    "task.message_sent": "working",
    "task.approval_resolved": "working",
    "approval.resolved": "working",
    "task.needs_input": "attention",
    "task.approval_required": "attention",
    "task.completed": "done",
    "task.failed": "failed",
}

# Words routed into a worker: the moment worth a flash. Creation is not
# here - a new worker raises its own window (focus_on_create).
_FLASHES = ("task.message_sent",)

# The two directions of the delegation loop. Boss -> worker is the brief
# and every message after it; worker -> Boss is each moment the worker
# hands the ball back - a finish, a failure, a question it cannot answer.
_ROUTED_IN = ("task.created", "task.message_sent")
_REPORTED_BACK = ("task.completed", "task.failed",
                  "task.needs_input", "task.approval_required")


@dataclass
class _Job:
    name: str
    task_id: str
    state: str | None
    flash: bool
    status: str | None = None
    # A job for the Boss's own workspace names a worker's task but must
    # not take its title or colour.
    entitle: bool = True


class WorkspaceDecor:
    """Follows the bus and dresses workers' workspaces accordingly."""

    # A fresh task's workspace is not listable the second task.created
    # fires - measured live, it appears about five seconds later. Missing
    # that first dress leaves the bar untitled and spinnerless until some
    # later event, so a miss is retried on this thread's own clock; the
    # queue can afford the pause, tasks cannot be touched by it.
    RETRIES = 3
    RETRY_DELAY = 2.0

    def __init__(self, conductor, runtime) -> None:
        self.conductor = conductor
        self.runtime = runtime
        # Workspaces already carrying their human title. A title is set
        # once and then left alone: the user may rename a workspace, and
        # re-stamping it on every event would fight them for it.
        self._titled: set[str] = set()
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._thread = threading.Thread(target=self._drain, daemon=True,
                                        name="workspace-decor")
        self._thread.start()
        self._unsubscribe = conductor.bus.subscribe(self.handle_event)

    def close(self) -> None:
        self._unsubscribe()
        self._queue.put(None)
        self._thread.join(timeout=5)

    def handle_event(self, event: ObservabilityEvent) -> None:
        if not event.task_id or event.task_id == BOSS_TASK_ID:
            return
        state = _STATES.get(event.type)
        flash = event.type in _FLASHES
        if state is None and not flash:
            return
        worker, boss = self._route(event)
        self._queue.put(_Job(session_name(event.task_id), event.task_id,
                             state, flash, status=worker))
        if boss:
            self._queue.put(_Job(session_name(BOSS_TASK_ID), event.task_id,
                                 None, False, status=boss, entitle=False))

    def _route(self, event: ObservabilityEvent) -> tuple[str | None,
                                                         str | None]:
        """The pill for each end of the loop, stamped with the hour."""
        when = time.strftime("%H:%M")
        if event.type in _ROUTED_IN:
            return (f"\u25c0 Boss {when}",
                    f"\u25b6 {self._short(event.task_id)} {when}")
        if event.type in _REPORTED_BACK:
            return (f"\u25b6 Boss {when}",
                    f"\u25c0 {self._short(event.task_id)} {when}")
        return None, None

    def _short(self, task_id: str) -> str:
        try:
            _, task = self.conductor._find_task(task_id)
        except KeyError:
            return "worker"
        title = task.title or "worker"
        return title if len(title) <= 24 else title[:23] + "\u2026"

    def _title(self, task_id: str) -> str | None:
        try:
            _, task = self.conductor._find_task(task_id)
        except KeyError:
            return None
        project = self.conductor.projects.get(task.project_id)
        label = project.display_name if project else ""
        return f"{label}: {task.title}" if label else task.title

    def _drain(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            # The title is decided here, on the one thread that paints,
            # so "already titled" cannot race the event that titles it.
            title = (None if not job.entitle or job.name in self._titled
                     else self._title(job.task_id))
            try:
                dressed = False
                for attempt in range(1 + self.RETRIES):
                    if attempt:
                        time.sleep(self.RETRY_DELAY)
                    dressed = self.runtime.dress(job.name, title=title,
                                                 state=job.state,
                                                 flash=job.flash,
                                                 status=job.status)
                    if dressed:
                        break
                if dressed and title:
                    self._titled.add(job.name)
                if not dressed:
                    # Not an error - the next event re-dresses it - but a
                    # worker that stays cryptic in the sidebar must be
                    # findable in the log.
                    application_log(
                        "workspace", "workspace.dress_missed",
                        f"{job.name!r} has no workspace to dress",
                        severity="debug", task_id=job.task_id)
            except Exception:
                application_log("workspace", "workspace.dress_failed",
                                f"could not dress {job.name!r}'s workspace",
                                severity="warning", exc_info=True)
