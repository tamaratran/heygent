"""The Conductor: the deterministic operating system under the Manager model.

Exposes exactly the seven V1 orchestration operations the Manager may call:

    create_task, list_tasks, inspect_task, send_to_task,
    interrupt_task, resume_task, cancel_task

The Manager resolves "the login one" to a task id; everything from the task id
down - session ids, worktrees, subscriptions, persistence - happens here and
never inside a model (Invariants 3, 4, 9).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path

from . import computer, task_context, task_events
from .agent_events import AgentEvent, SUMMARY_CEILING, keep_end
from .boss_tools import check_title
from .subagent_state import (SubagentState, SubagentStateStore,
                             TERMINAL as SUBAGENT_TERMINAL,
                             apply_lifecycle, dismiss, reduce)
from .subagents import subagent_status
from .manager import ManagerBackend, ManagerTurn, task_summary
from .observability import ObservabilityBus, ObservabilityEvent, new_trace
from .runtime import CodingAgentRuntime
from .task_store import TaskStore
from .task_types import now_iso, Task, Workspace
from .workspaces import SharedWorkspaceManager, WorkspaceManager

# The complete manager tool surface. handle_action() accepts these names and
# nothing else - the Manager model never gets a bigger vocabulary than this.
MANAGER_TOOLS = ("create_task", "list_tasks", "inspect_task", "send_to_task",
                 "interrupt_task", "resume_task", "complete_task",
                 "cancel_task")


def build_worker_prompt(task: Task, project_context: str,
                        workspace: Workspace | None = None) -> str:
    """The first message a fresh worker session receives: the goal as the
    Boss wrote it, then the two machine rules. The user asked for their own
    prompt and not a wrapper around it, so nothing else goes in - the CLI
    already states its working directory and branch, and the title is for
    the card, not the worker. `workspace` is accepted for callers that
    still pass it."""
    parts = [task.goal.strip() or task.title]
    if project_context.strip():
        parts.append(project_context.strip())
    parts.append(computer.worker_brief() if task.computer
                 else SHARED_MACHINE_RULE)
    parts.append(ASK_THEN_WAIT_RULE)
    return "\n\n".join(parts)


# Every worker gets this, in every brief. It exists because a worker
# investigating barge-in synthesised a voice clip counting to forty, opened
# its own Live session, and played the model's reply through the speakers
# while the user was mid-conversation with the product. Nothing had told it
# the machine was in use. The rule is about the machine, not the task, so
# it is not left to the manager to remember per task. The exception exists
# because things opened with -g landed behind the voice session.
SHARED_MACHINE_RULE = (
    "The user is on this machine, talking to a voice assistant: do not "
    "play audio, use the microphone, take focus or type into other apps; "
    "test those paths with files and fakes. If you open something for "
    "the user, bring it to the front (no -g) unless they asked for it in "
    "the background.")

# The user answers by voice, relayed through a manager, so an answer takes
# seconds to minutes to arrive as the next message. A worker that asked
# "badge only, or the full CI workflow?" and then wrote both files while
# waiting had its work reverted when the answer picked one.
ASK_THEN_WAIT_RULE = (
    "If you need the user's decision, ask in plain text (no option menus - "
    "they answer by voice) and end your turn there; do not act on your "
    "own recommendation before the answer comes.")


class Conductor:
    def __init__(self, project_root: str | Path,
                 runtime: CodingAgentRuntime,
                 workspaces: WorkspaceManager | None = None,
                 store: TaskStore | None = None,
                 bus: ObservabilityBus | None = None,
                 manager: ManagerBackend | None = None) -> None:
        self.root = Path(project_root).resolve()
        self.runtime = runtime
        self.bus = bus or ObservabilityBus()   # no subscribers = silent no-op
        self.store = store or TaskStore(self.root)
        self.workspaces = workspaces or SharedWorkspaceManager(self.root)
        self.manager = manager
        self._unsubscribes: dict[str, callable] = {}
        # Canonical Managed Subagent state, one file per task beside the
        # task's own. The reducer writes it; the card and the inbox read
        # it; nothing else does.
        self.subagents = SubagentStateStore(
            lambda task_id: self.store.task_dir(task_id) / "subagent.json")
        # Lifecycle decisions (pause, cancel, close, interrupt, recover)
        # change the Task through the store, which announces every status
        # change on the bus. That one subscription keeps canonical state
        # in step with all of them.
        self._unsubscribe_status = self.bus.subscribe(self._on_task_status)

    # -- helpers ------------------------------------------------------------
    def _emit(self, event_type: str, component: str = "conductor",
              **kwargs) -> None:
        self.bus.emit(ObservabilityEvent(type=event_type,
                                         component=component, **kwargs))
    def project_context(self) -> str:
        try:
            return self.store.project_md_path().read_text()
        except OSError:
            return ""

    def _require(self, task_id: str) -> Task:
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        return task

    def _on_event(self, task_id: str):
        """Wire one task's AgentEvents into state, history, and context.

        Deterministic mapping only: a finished turn means the worker is idle
        and waiting for the user, not that the task is done - marking a task
        completed is a Manager/user decision. Every finished turn also
        refreshes the task's structured result from the worker's own events
        (summary plus the latest checkpoint's findings/files/next steps), so
        the Manager never reconstructs outcomes from transcripts.
        """
        # The most recent checkpoint enriches the result when the turn ends.
        last_checkpoint: dict = {}

        def handler(event: AgentEvent) -> None:
            # Canonical state first. The reducer decides whether this event
            # means anything - a replay, a late arrival and a straggler
            # after a terminal state all change nothing - and only an
            # event that changed state reaches the side effects below.
            current = self.store.get(task_id)
            if current is None:
                return
            state = self._subagent_state(current)
            if state.status in SUBAGENT_TERMINAL and \
                    current.status not in ("completed", "failed",
                                           "cancelled"):
                # The event in hand is proof the worker is alive, and a
                # non-terminal Task says the product still counts it as
                # active - so a terminal sidecar is a leftover, not a
                # verdict. The running-edge revival below (_on_task_status)
                # repairs this pair only when the edge fires in THIS
                # process: task_987f66d8 was retired and revived under one
                # conductor, the restart started the next with
                # Task=running/sidecar=completed already in place, the
                # status never changed again, and 13+ events - two turn
                # ends among them - dropped as `terminal` (2026-08-31
                # 22:15-22:19Z). Level, not edge: any event from a live
                # worker on an active task un-mutes it.
                state = apply_lifecycle(state, "working")
            state, transition = reduce(state, event)
            if transition.outcome in ("duplicate", "stale", "terminal"):
                self._emit("agent.event_dropped", "task", task_id=task_id,
                           provider_session_id=current.provider_session_id,
                           data={"reason": transition.outcome,
                                 "event_id": event.event_id,
                                 "sequence": event.sequence,
                                 "kind": event.type})
                return
            self.subagents.save(state)
            self._emit("subagent.state_changed", "task", task_id=task_id,
                       provider_session_id=current.provider_session_id,
                       data={"state": state.to_dict(),
                             "transition": {
                                 "outcome": transition.outcome,
                                 "from": transition.before,
                                 "to": transition.after,
                                 "changed": list(transition.changed),
                                 "event_id": event.event_id,
                                 "sequence": event.sequence,
                                 "kind": event.type}})
            # Lifecycle regression guard on the Task itself, kept for the
            # branches below: completed -> working is invalid there too.
            if current.status in ("completed", "failed", "cancelled"):
                return
            # A paused task records history but its deliberate suspension is
            # only lifted by an explicit resume, never by a stray event.
            paused = current is not None and current.status == "paused"
            fields = {}
            if event.summary:
                fields["summary"] = event.summary
            if event.error:
                fields["error"] = event.error
            if event.question:
                fields["question"] = event.question
            if event.detail:
                fields["detail"] = event.detail
            task_events.append(self.store, task_id, event.type, **fields)
            if paused and event.type not in ("failed",):
                return               # recorded, but no lifecycle movement
            if event.type == "started":
                self.store.update(task_id, status="running")
                self._emit("task.started", "task", task_id=task_id)
            elif event.type in ("completed", "needs_input",
                                "approval_required"):
                self.store.update(task_id, status="waiting_for_user")
                if event.summary:
                    task_context.update_section(self.store, task_id,
                                                "Current Status",
                                                event.summary[:800])
                if event.type == "completed":
                    from .subagents import SubagentResult
                    detail = last_checkpoint
                    result = SubagentResult(
                        summary=keep_end(event.summary, SUMMARY_CEILING)
                        or "Done.", success=True,
                        files_changed=detail.get("files_changed", [])[:12],
                        findings=detail.get("findings", [])[:8],
                        next_steps=detail.get("next_steps", [])[:8],
                        warnings=detail.get("warnings", [])[:8])
                    task = self.store.update(task_id,
                                             result=result.to_dict())
                    # The hard invariant: a finished user-facing worker
                    # produces one structured result for the Manager and one
                    # user-visible completion notification, tied to this
                    # exact task and session - pushed from the runtime
                    # lifecycle event, never inferred from silence. The
                    # summary goes whole: what the user is shown of it
                    # is cut by the surface that shows it.
                    self._emit("task.completed", "task", task_id=task_id,
                               provider_session_id=task.provider_session_id,
                               data={"summary": result.summary,
                                     "success": True,
                                     "warnings": result.warnings})
                if event.type == "approval_required":
                    approval = (event.detail or {}).get("approval", {})
                    self._emit("task.approval_required", "task",
                               task_id=task_id,
                               data={"question": event.question[:300],
                                     "approval": approval})
                    self._emit("approval.detected", "runtime",
                               task_id=task_id,
                               data={"approval_id":
                                     approval.get("approval_id", ""),
                                     "description": event.question[:200]})
                elif event.type == "needs_input":
                    # A blocked worker outranks an idle one: surfaces treat
                    # an open question as needs-attention, not a milestone.
                    self._emit("task.needs_input", "task", task_id=task_id,
                               data={"question": event.question[:300]})
                # completed already emitted task.completed above: one
                # completion, one notification - never a second milestone.
            elif event.type == "approval_resolved":
                # Provider-side acknowledgement: the decision reached the
                # live worker and it advanced past the gate. Only now is the
                # approval truly handled.
                detail = event.detail or {}
                self.store.update(task_id, status="running")
                self._emit("approval.resolved", "runtime", task_id=task_id,
                           data={"approval_id": detail.get("approval_id", ""),
                                 "decision": detail.get("decision", ""),
                                 "resolved_by": detail.get("resolved_by",
                                                           "conductor")})
            elif event.type == "failed":
                from .subagents import SubagentResult
                self.store.update(task_id, status="failed",
                                  result=SubagentResult(
                                      summary=keep_end(event.error,
                                                       SUMMARY_CEILING)
                                      or "Failed.",
                                      success=False).to_dict())
                self._emit("task.failed", "task", task_id=task_id,
                           severity="error", data={"error": event.error[:200]})
            elif event.type == "checkpoint":
                last_checkpoint.clear()
                last_checkpoint.update(event.detail or {})
                # A worker's report_progress call: semantic memory updates,
                # no transcript summarization by another model (spec 48).
                detail = event.detail or {}
                if event.summary:
                    task_context.update_section(self.store, task_id,
                                                "Current Status",
                                                event.summary[:800])
                if detail.get("next_steps"):
                    task_context.update_section(
                        self.store, task_id, "Next Steps",
                        "\n".join(f"- {step}"
                                  for step in detail["next_steps"][:8]))
                if detail.get("findings"):
                    task_context.append_findings(self.store, task_id,
                                                 detail["findings"])
                self._emit("task.context_updated", "task", task_id=task_id,
                           data={"summary": event.summary[:200]})
            elif event.type == "progress":
                # Carry the source through. The runtime already knows the
                # difference between the worker narrating itself and a
                # human typing an instruction into its window - it tags
                # the latter "user_message" - and dropping that here made
                # the two indistinguishable everywhere above. Anything
                # deciding whether an answer was ASKED FOR could then only
                # see instructions the conductor itself sent.
                self._emit("runtime.progress", "runtime", task_id=task_id,
                           data={"summary": event.summary[:200],
                                 "source": (event.detail or {}).get(
                                     "source", "")})
        return handler

    def _subagent_state(self, task: Task) -> SubagentState:
        """The canonical state for a task, created from the Task the first
        time (a task that predates the sidecar, or a fresh one)."""
        state = self.subagents.get(task.id)
        if state is not None:
            return state
        return SubagentState(
            id=f"sub_{task.id}", task_id=task.id, project_id=task.project_id,
            provider=task.provider,
            provider_session_id=task.provider_session_id, title=task.title,
            status=subagent_status(task, None, False),
            result=task.result)

    def subagent_state_dict(self, task_id: str) -> dict | None:
        """What the card and the Boss read. None if the task is unknown."""
        task = self.store.get(task_id)
        if task is None:
            return None
        return self._subagent_state(task).to_dict()

    def dismiss_card(self, task_id: str) -> dict | None:
        """The user waved the worker's card away: recorded in canonical
        state, persisted, so the card stays away until a result or a
        question arrives after this point - and across restarts."""
        task = self.store.get(task_id)
        if task is None:
            return None
        state = dismiss(self._subagent_state(task))
        self.subagents.save(state)
        self._emit("subagent.card_dismissed", "task", task_id=task_id,
                   provider_session_id=task.provider_session_id,
                   data={"revision": state.dismissed_at_revision})
        return state.to_dict()

    # Task statuses that only a decision produces, and what the worker's
    # canonical status becomes. Event-driven statuses (running,
    # waiting_for_user, failed) are the reducer's and are not mapped here.
    _LIFECYCLE = {"paused": "paused", "interrupted": "interrupted",
                  "completed": "completed", "cancelled": "cancelled"}

    def _on_task_status(self, event) -> None:
        if event.type != "task.status_changed" or not event.task_id:
            return
        task = self.store.get(event.task_id)
        if task is None:
            return                          # another project's task
        to = event.data.get("to")
        state = self.subagents.get(task.id)
        if state is None:
            # No canonical state yet - a task from before the sidecar, or
            # one whose worker never emitted. The Task has ALREADY moved by
            # the time this fires, so the prior state has to be read from
            # where the Task came from, or "completed -> completed" looks
            # like nothing happened and no card is ever drawn.
            from dataclasses import replace as _replace
            prior = _replace(task, status=event.data.get("from") or task.status)
            state = self._subagent_state(prior)
        if to in self._LIFECYCLE:
            new = apply_lifecycle(state, self._LIFECYCLE[to])
        elif to == "running" and state.status in ("paused", "interrupted",
                                                  "recovering", "failed",
                                                  "completed", "cancelled"):
            # Resumed, recovered - or revived. The terminal statuses are
            # the reducer's own verdicts and final for events, which is
            # right for a worker that is gone and wrong for one written
            # off while its process lived on - and every status here has
            # done that. "failed": one bad host listing said "tmux
            # session ended" for every session at once (2026-08-29
            # 15:16:58Z, 22:30:55Z), the workers went on running, the
            # Boss went on sending them work - and every report they
            # made after that was dropped as `terminal`: 85 events, two
            # finishes the user watched happen and never heard about.
            # "completed"/"cancelled" - what retirement closes a task
            # as: the idle-retire sweep closed task_987f66d8 (2026-08-31
            # 21:00:24Z, 19.6h unaddressed), the Boss sent it a
            # follow-up thirteen minutes later, the worker answered on
            # screen - and the turn end was dropped as `terminal`, so
            # no Worker update was ever typed into the Boss. A task
            # goes back to running only through send_to_task or
            # resume_task, both of which reach the session before the
            # status moves; running means alive.
            new = apply_lifecycle(state, "working")
        else:
            return
        if new.status == state.status:
            return
        new.provider_session_id = task.provider_session_id
        self.subagents.save(new)
        self._emit("subagent.state_changed", "task", task_id=task.id,
                   provider_session_id=task.provider_session_id,
                   data={"state": new.to_dict(),
                         "transition": {"outcome": "lifecycle",
                                        "from": state.status,
                                        "to": new.status,
                                        "changed": ["status"],
                                        "event_id": event.event_id,
                                        "sequence": 0,
                                        "kind": f"task.{to}"}})

    async def reconcile_stuck_pair(self, task_id: str) -> bool:
        """Repair a Task still `running` whose sidecar is terminal.

        The pair cannot arise by the rules - a running Task means a live
        worker, and events from a live worker keep its sidecar moving -
        so finding it means an edge was lost: the retire-and-revive of
        task_987f66d8 happened under one conductor, the restart started
        the next with the pair already inconsistent, and the card said
        Running for a worker that had answered and gone quiet an hour
        before. The event handler heals this when the worker next speaks;
        this heals it when it does not - the sweep's job, since nothing
        else is looking.

        The sidecar revives (the same lifecycle verb the running edge
        applies), and the Task stops claiming `running` when the runtime
        says the worker is idle at its prompt: the turn it finished is
        behind the watcher's offset and will never replay, so the truth
        - waiting_for_user - has to be said here. No update is pushed:
        an adopted finish is old news (see _readopt's rule).
        """
        task = self.store.get(task_id)
        if task is None or task.status != "running":
            return False
        state = self.subagents.get(task_id)
        if state is None or state.status not in SUBAGENT_TERMINAL:
            return False
        seen = await self.runtime.get_status(task.provider_session_id) \
            if task.provider_session_id else ""
        # Idle at its prompt: the worker answered and stands by, so the
        # sidecar goes to idle (rendered with the result it already
        # holds) and the Task to waiting_for_user. Anything else: mid
        # turn, or cannot tell - working, and the Task keeps running.
        new = apply_lifecycle(state, "idle" if seen == "idle" else "working")
        new.provider_session_id = task.provider_session_id
        self.subagents.save(new)
        self._emit("subagent.state_changed", "task", task_id=task_id,
                   provider_session_id=task.provider_session_id,
                   data={"state": new.to_dict(),
                         "transition": {"outcome": "lifecycle",
                                        "from": state.status,
                                        "to": new.status,
                                        "changed": ["status"],
                                        "event_id": "",
                                        "sequence": 0,
                                        "kind": "reconcile.stuck_pair"}})
        if seen == "idle":
            self.store.update(task_id, status="waiting_for_user")
        return True

    async def _subscribe(self, task: Task) -> None:
        if task.id in self._unsubscribes or not task.provider_session_id:
            return
        unsubscribe = await self.runtime.subscribe(task.provider_session_id,
                                                   self._on_event(task.id))
        self._unsubscribes[task.id] = unsubscribe

    def _unsubscribe(self, task_id: str) -> None:
        unsubscribe = self._unsubscribes.pop(task_id, None)
        if unsubscribe:
            unsubscribe()

    # -- the seven manager tools -----------------------------------------
    async def create_task(self, title: str, goal: str,
                          location: str | None = None,
                          computer: bool = False,
                          provider: str | None = None) -> Task:
        """The creation pipeline, persisted before any external work so a
        crash at any step leaves recoverable state.

        location says where this one worker runs. It reaches the runtime as
        a request rather than an argument, because only a router can honour
        it and the protocol is shared with runtimes that cannot."""
        title = check_title(title)
        provider = provider or "claude-code"
        task = self.store.create(title=title, goal=goal, computer=computer,
                                 provider=provider)
        # Which CLI, and where. A provider other than Claude Code names
        # its own runtime (one per CLI, docs/any-cli.md); Claude Code is
        # the one with a choice of place.
        want = getattr(self.runtime, "want", None)
        if callable(want):
            if provider != "claude-code":
                want(task.id, provider)
            elif location:
                want(task.id, location)
        task_events.append(self.store, task.id, "task_created",
                           title=title, goal=goal)
        self._emit("task.created", "task", task_id=task.id,
                   data={"title": title})
        self.store.update(task.id, status="starting")
        session_id = None
        try:
            self._emit("workspace.creating", "workspace", task_id=task.id)
            # A worktree is a git subprocess; the runtime's session is many
            # more. None of them may hold the loop - the microphone gate
            # and the Fn key are read from it.
            workspace = await asyncio.to_thread(self.workspaces.create,
                                                task.id)
            self.store.update(task.id, workspace=workspace)
            self._emit("workspace.created", "workspace", task_id=task.id,
                       data={"path": workspace.path,
                             "isolation": workspace.isolation_type})
            task_context.create_initial(self.store, task)
            session_id = await self.runtime.create_session(
                task_id=task.id, working_directory=workspace.path,
                initial_prompt=build_worker_prompt(task,
                                                   self.project_context(),
                                                   workspace))
            task = self.store.update(task.id, provider_session_id=session_id,
                                     status="running")
            task_events.append(self.store, task.id, "agent_started",
                               provider_session_id=session_id)
            self._emit("runtime.session_created", "runtime", task_id=task.id,
                       provider_session_id=session_id)
            # The debuggable identity chain: task -> session -> workspace.
            # A surface later shows this exact execution's transcript.
            self._emit("execution.mapped", "runtime", task_id=task.id,
                       provider_session_id=session_id,
                       data={"workspace": workspace.path})
            await self._subscribe(task)
        except Exception as exc:
            self.store.update(task.id, status="failed")
            task_events.append(self.store, task.id, "failed", error=str(exc))
            self._emit("task.failed", "task", task_id=task.id,
                       severity="error", data={"error": str(exc)[:200]})
            if session_id is None:
                # No worker ever ran here, so there is nothing to come
                # back to: a task that failed to start is not resumable,
                # and its empty worktree was the most common kind left
                # behind. A worker that started and then died is a
                # different case - recovery resumes it in this directory.
                if await self._still_hosted(task.id):
                    # Unless the host still has something running for
                    # it. Measured 2026-08-29: a launch reported failed
                    # had in fact started, and releasing here deleted the
                    # worktree out from under a working claude.
                    self._emit("workspace.kept", "workspace", task_id=task.id,
                               severity="warning",
                               data={"reason": "failed_to_start",
                                     "error": "the runtime still hosts a "
                                              "session for this task"})
                else:
                    self._release_workspace(task.id, "failed_to_start")
            raise
        return task

    async def _still_hosted(self, task_id: str) -> bool:
        """Whether the runtime still has a PTY for a task it says failed
        to start. Runtimes without the question answer no."""
        alive = getattr(self.runtime, "session_alive", None)
        if not callable(alive):
            return False
        from .tmux_runtime import session_name
        try:
            return bool(await asyncio.to_thread(alive, session_name(task_id)))
        except Exception:
            return False

    def _release_workspace(self, task_id: str, reason: str) -> bool:
        """Give a finished task's working directory back, when that is safe.

        cleanup() is non-forcing on purpose: a worktree with uncommitted
        changes is refused, and the refusal is the point. The branch always
        survives, so committed work is never at stake; uncommitted work is
        the one thing only the directory holds, and a task ending is not
        permission to throw it away. A kept directory is reported rather
        than left silently - that is how 44 of them accumulated.
        """
        try:
            self.workspaces.cleanup(task_id)
        except Exception as exc:
            self._emit("workspace.kept", "workspace", task_id=task_id,
                       severity="warning",
                       data={"reason": reason, "error": str(exc)[:200]})
            return False
        self._emit("workspace.released", "workspace", task_id=task_id,
                   data={"reason": reason})
        return True

    def list_tasks(self) -> list[Task]:
        return self.store.list()

    def inspect_task(self, task_id: str, recent_events: int = 10) -> dict:
        task = self._require(task_id)
        return {
            "task": task.to_dict(),
            "context": task_context.read(self.store, task_id),
            "recent_events": task_events.read(self.store,
                                              task_id)[-recent_events:],
        }

    async def _ensure_session(self, task: Task) -> None:
        """Wake a dormant worker just before it is needed.

        resume() is idempotent in both runtimes - it returns immediately when
        the session is already live - so this is cheap on the common path and
        is what lets startup leave everything asleep.
        """
        if not task.provider_session_id:
            raise RuntimeError(f"{task.id} has no provider session")
        cwd = task.workspace.path if task.workspace else str(self.root)
        await self.runtime.resume(task.provider_session_id,
                                  working_directory=cwd)
        await self._subscribe(task)

    async def send_to_task(self, task_id: str, message: str) -> None:
        """A meaningful instruction reaches both the live session and the
        durable context, so a replacement worker still honours it."""
        task = self._require(task_id)
        await self._ensure_session(task)
        await self.runtime.send(task.provider_session_id, message)
        self.store.update(task_id, status="running",
                          last_instruction_at=now_iso())
        task_context.append_instruction(self.store, task_id, message)
        task_events.append(self.store, task_id, "user_instruction",
                           text=message)
        self._emit("task.message_sent", "task", task_id=task_id,
                   provider_session_id=task.provider_session_id)

    async def interrupt_task(self, task_id: str) -> None:
        task = self._require(task_id)
        if task.provider_session_id:
            await self.runtime.interrupt(task.provider_session_id)
        self.store.update(task_id, status="waiting_for_user")
        task_events.append(self.store, task_id, "interrupted")
        self._emit("task.interrupted", "task", task_id=task_id)

    async def resume_task(self, task_id: str) -> None:
        task = self._require(task_id)
        if not task.provider_session_id:
            raise RuntimeError(f"{task_id} has no provider session to resume")
        cwd = task.workspace.path if task.workspace else str(self.root)
        await self.runtime.resume(task.provider_session_id,
                                  working_directory=cwd)
        self.store.update(task_id, status="running")
        task_events.append(self.store, task_id, "resumed")
        self._emit("task.resumed", "task", task_id=task_id,
                   provider_session_id=task.provider_session_id)
        await self._subscribe(self.store.get(task_id))

    async def complete_task(self, task_id: str) -> None:
        """The task is done - because the user, through the Manager, says so.

        Nothing else ever says so. A PTY worker does not exit when it has
        answered: it ends a turn and waits, because the next thing the user
        says may be a follow-up. So the runtime's `completed` event is a
        turn end and maps to waiting_for_user, and until now the terminal
        `completed` status - in TASK_STATUSES from the start, treated as
        finished by every consumer - had no producer at all. The only ways
        a task ever ended were cancel and a crash, which is why every
        finished task kept its window, its session and its worktree.

        Marks the task; the session and its directory stay. Measured
        2026-09-01: the Boss now completes a task within seconds of its
        worker answering, and completion killed the pane, so every card
        the user opened afterwards was a dead terminal under "The
        session has ended". A finished worker is worth reading, and the
        user said so - old windows may stay. close_pane() is the
        teardown, run by the watchdog once the task has sat unread for
        the idle-retire window, or at once when the sweep retires an
        idle task. The branch and the context survive either way: the
        work is the branch. Idempotent, like the other lifecycle verbs.
        """
        task = self._require(task_id)
        self._unsubscribe(task_id)
        self.store.update(task_id, status="completed")
        task_events.append(self.store, task_id, "completed")
        # Not task.completed: that event is a worker's turn end and carries
        # a notification with it. This is the task closing, and the user
        # is the one who closed it.
        self._emit("task.closed", "task", task_id=task_id,
                   provider_session_id=task.provider_session_id)

    async def close_pane(self, task_id: str, reason: str = "finished") -> bool:
        """Tear down a finished task's session and give its directory
        back - the part completion used to do at once. True if there was
        a session to end."""
        task = self._require(task_id)
        ended = False
        if task.provider_session_id:
            try:
                await self.runtime.destroy(task.provider_session_id)
                ended = True
            except Exception:
                pass                 # already gone is the desired state
        self._release_workspace(task_id, reason)
        self._emit("task.pane_closed", "task", task_id=task_id,
                   provider_session_id=task.provider_session_id,
                   data={"reason": reason})
        return ended

    async def cancel_task(self, task_id: str) -> None:
        """Stop and forget the session. The branch and the context survive,
        and so does a directory holding uncommitted work - discarding work
        is a separate, explicit decision. A directory holding nothing the
        branch does not is given back."""
        task = self._require(task_id)
        self._unsubscribe(task_id)
        if task.provider_session_id:
            try:
                await self.runtime.destroy(task.provider_session_id)
            except Exception:
                pass                 # already gone is fine; cancelled is cancelled
        self.store.update(task_id, status="cancelled")
        task_events.append(self.store, task_id, "cancelled")
        self._emit("task.cancelled", "task", task_id=task_id)
        self._release_workspace(task_id, "cancelled")

    # -- the manager seam ---------------------------------------------------
    async def handle_action(self, tool: str, args: dict | None = None):
        """Execute one validated Manager tool call.

        This is the single entry point the Manager (and later the voice
        layer, via the Manager) drives. The model chooses a tool name and
        semantic arguments; everything after this line is deterministic.
        """
        args = args or {}
        self._emit("conductor.action_received",
                   data={"tool": tool, "args": args})
        if tool not in MANAGER_TOOLS:
            self._emit("conductor.error", severity="error",
                       data={"tool": tool, "error": "unknown tool"})
            raise ValueError(f"unknown manager tool: {tool!r}")
        if "task_id" in args:
            task = self.store.get(args["task_id"])
            if task is None:
                self._emit("conductor.error", severity="error",
                           data={"tool": tool, "error": "unknown task",
                                 "task_id": args["task_id"]})
                raise KeyError(f"no such task: {args['task_id']}")
            self._emit("conductor.task_resolved", task_id=task.id,
                       provider_session_id=task.provider_session_id)
        started = time.monotonic()
        try:
            result = getattr(self, tool)(**args)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            self._emit("conductor.error", severity="error",
                       data={"tool": tool, "error": str(exc)[:200]})
            raise
        self._emit("conductor.action_executed",
                   task_id=args.get("task_id"),
                   duration_ms=round((time.monotonic() - started) * 1000, 1),
                   data={"tool": tool})
        return result

    async def handle_user_message(self, text: str,
                                  source: str = "text",
                                  trace_id: str = "") -> ManagerTurn:
        """The single entry point for user input - typed, tested, or spoken.

        Voice (Phase 7) will call exactly this, so everything below the
        transcript is testable without a microphone.
        """
        if self.manager is None:
            raise RuntimeError("no ManagerBackend configured")
        new_trace(trace_id)
        started = time.monotonic()
        # The task snapshot makes a trace self-contained: replay and
        # trace-to-eval can rebuild exactly the registry the Manager saw.
        self._emit("manager.turn_started", "manager",
                   data={"source": source, "text": text[:300],
                         "tasks": [task_summary(t) for t in self.list_tasks()]})
        try:
            turn = await self.manager.handle(text, self)
        except Exception as exc:
            self._emit("manager.error", "manager", severity="error",
                       data={"error": str(exc)[:300]})
            raise
        for call in turn.tool_calls:
            self._emit("manager.tool_call", "manager",
                       task_id=call.args.get("task_id"),
                       data={"tool": call.tool, "args": call.args})
        self._emit("manager.turn_completed", "manager",
                   duration_ms=round((time.monotonic() - started) * 1000, 1),
                   data={"reply": turn.reply[:300],
                         "tools": [call.tool for call in turn.tool_calls]})
        return turn

    # -- startup recovery -----------------------------------------------------
    async def startup(self, resume: bool = False) -> list[Task]:
        """Take stock of what survived the restart, waking nothing.

        Launching must not spawn workers. Every finished turn leaves a task
        in waiting_for_user, so resuming that state at startup resurrected
        every task ever created - a terminal per task, every launch. Sessions
        are now revived on demand instead, by _ensure_session below.

        Pass resume=True to reconnect eagerly (recovery drills, tests).
        """
        self._emit("storage.recovery_started", "storage")
        recovered = []
        if not resume:
            dormant = self.store.running_tasks()
            self._emit("storage.recovery_completed", "storage",
                       data={"recovered": 0, "dormant": len(dormant)})
            return recovered
        for task in self.store.running_tasks():
            try:
                if not task.provider_session_id:
                    raise RuntimeError("no provider session recorded")
                cwd = (task.workspace.path if task.workspace
                       else str(self.root))
                await self.runtime.resume(task.provider_session_id,
                                          working_directory=cwd)
                await self._subscribe(task)
                task_events.append(self.store, task.id, "reconnected")
                self._emit("runtime.session_resumed", "runtime",
                           task_id=task.id,
                           provider_session_id=task.provider_session_id)
                recovered.append(task)
            except Exception as exc:
                self.store.update(task.id, status="waiting_for_user")
                task_events.append(self.store, task.id, "session_lost",
                                   error=str(exc))
                self._emit("runtime.session_missing", "runtime",
                           task_id=task.id, severity="warning",
                           data={"error": str(exc)[:200]})
        self._emit("storage.recovery_completed", "storage",
                   data={"recovered": len(recovered)})
        return recovered
