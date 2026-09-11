"""The Global Conductor: no project is selected before the user speaks.

Composes the project plane (ProjectStore + ProjectLocator) over the existing
per-project task machinery. The Manager gets eleven tools - four project
tools and the seven task tools - and everything below a resolved id stays
deterministic:

    "Fix login in Posely"
        find_project("Posely")     <- locator candidates, model selects
        create_task(proj_..., ...) <- pipeline, workspace, session

Task ids are globally unique, so task tools take a task_id and the owning
project is resolved from local state, never by the model (Invariant 8: task
operations are explicitly project-scoped internally).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path
from typing import Callable

from . import instance, task_events
from .capabilities import PROVIDER_NAMES, computer_state
from .conductor import Conductor, build_worker_prompt
from .locator import ProjectLocator
from datetime import datetime, timezone

from .manager import idle_for, ManagerBackend, ManagerTurn, task_summary
from .observability import (ObservabilityBus, ObservabilityEvent,
                            application_log, current_run, new_trace)
from .projects import Project, ProjectStore
from .runtime import ApprovalPolicy, CodingAgentRuntime
from .surfaces import (SessionSurface, SurfaceHandle, SurfacePreference,
                       SurfaceRequest)
from .task_store import TaskStore
from .storage import now_iso
from .task_types import Task
from .workspaces import SharedWorkspaceManager, WorkspaceManager

# No find_project or register_project (removed 2026-09-10): create_task
# resolves and registers a project from its name or path, and an ambiguous
# name comes back as an error listing the candidates' paths. The methods
# stay; the Boss just is not handed them.
PROJECT_TOOLS = ("list_projects", "inspect_project")
TASK_TOOLS = ("create_task", "list_tasks", "list_subagents", "inspect_task",
              "send_to_task", "pause_task", "interrupt_task", "resume_task",
              "complete_task", "cancel_task", "handoff_task_context",
              "focus_task",
              "approve_task_action", "deny_task_action",
              "list_open_sessions", "list_recent_sessions",
              "search_sessions", "situation")
# What the Manager tells the voice on the side. A note is not spoken and
# not typed anywhere; it reaches the voice model on the commentary
# channel, so "which file was that?" is answered from what the Manager
# already knew instead of waking it again.
VOICE_TOOLS = ("note_for_voice", "what_the_voice_said", "tell_user")
MANAGER_TOOLS = PROJECT_TOOLS + TASK_TOOLS + VOICE_TOOLS


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"

# Read-only tools may run freely during a turn; only these change the world.
MUTATING_TOOLS = ("create_task", "send_to_task", "interrupt_task",
                  "resume_task", "complete_task", "cancel_task",
                  "handoff_task_context")

ACTIVE_STATUSES = ("starting", "running", "waiting_for_user", "interrupted")


def default_workspace_factory(store: ProjectStore
                              ) -> Callable[[Project], WorkspaceManager]:
    """Worktrees under <home>/workspaces/<projectId>/ when the project is a
    git repo; the shared root otherwise."""
    def factory(project: Project) -> WorkspaceManager:
        from .workspaces import GitWorktreeManager
        repo = Path(project.repo_root or project.root_path)
        if (repo / ".git").is_dir():
            return GitWorktreeManager(
                repo, worktrees_dir=store.workspaces_dir() / project.id)
        return SharedWorkspaceManager(project.root_path)
    return factory


def _normalise(text: str) -> str:
    return " ".join(text.lower().split()).strip(" .!?,;:")


def _trailing_question(summary: str) -> str:
    """The question a finished turn ends on, or "". A worker that stops to
    ask ends its turn - the runtime reports `completed`, never
    `needs_input` - so the question is read off the summary's last
    sentence."""
    text = " ".join(summary.split())
    if not text.endswith("?"):
        return ""
    start = max(text.rfind(". "), text.rfind("! "), text.rfind("? ", 0, len(text) - 1))
    return text[start + 2:] if start >= 0 else text


def hold_to_users_words(message: str, utterance: str) -> tuple[str, str]:
    """What actually goes to the worker, and what the manager wrote if it
    was overruled ("" when it was not).

    The manager's message is kept when it IS the user's words - the whole
    utterance, or a part of it quoted for a task that only one part was
    about. Anything else is the manager's phrasing, and the user's own
    sentence goes instead: it has their emphasis, their terminology, and
    only the instructions they gave.

    The trade this makes, stated: one utterance aimed at two tasks reaches
    both in full. The manager can still route a part by quoting it.
    """
    spoken = _normalise(utterance)
    if not spoken:
        return message, ""            # nothing spoken: a typed or
                                      # manager-initiated turn, unchanged
    wrote = _normalise(message)
    if wrote and wrote in spoken:
        return message, ""            # their words, whole or a quoted part
    return utterance.strip(), message


# The task statuses in which a computer-use worker still holds the
# screen. waiting_for_user is one of them: a worker stopped on a question
# is mid-action, with a window under its cursor and half a form filled
# in, and taking the keyboard back there would strand it. paused,
# interrupted and the terminal three are not - nothing is going to move
# next, so nothing needs the keyboard.
ACTIVE_GUI_STATUSES = ("starting", "running", "waiting_for_user")


class GlobalConductor:
    # Defaults for an instance built without __init__ (tests do), and
    # the shape of the turn bookkeeping: see handle_user_message.
    _open_turns = 0
    _utterances: list[str] = ()      # replaced per instance before use
    _utterance = ""

    def __init__(self, home: str | Path | None,
                 runtime: CodingAgentRuntime,
                 manager: ManagerBackend | None = None,
                 bus: ObservabilityBus | None = None,
                 search_roots: list[str | Path] | None = None,
                 workspace_factory: Callable[[Project],
                                             WorkspaceManager] | None = None,
                 max_concurrent_tasks: int = 3,
                 surface: SessionSurface | None = None,
                 surfaces: dict[str, SessionSurface] | None = None,
                 surface_preference: SurfacePreference | None = None,
                 approval_policy: ApprovalPolicy | None = None,
                 idle_retire_s: float = 0.0,
                 ) -> None:
        self.projects = ProjectStore(home)
        # When this run began. The Activity Center's RECENT section shows
        # what finished during it - what this Boss did - not the last
        # eight finishes of all time.
        self.started_at = now_iso()
        # How long a task may sit unaddressed before the sweep closes it.
        # 0 means never: the roster then only shrinks when someone says so.
        self.idle_retire_s = float(idle_retire_s or 0.0)
        self.locator = ProjectLocator(self.projects, search_roots)
        self.runtime = runtime
        self.manager = manager
        self.bus = bus or ObservabilityBus()
        self.workspace_factory = (workspace_factory
                                  or default_workspace_factory(self.projects))
        self.max_concurrent_tasks = max_concurrent_tasks
        # Registered surface implementations by preference name. The single
        # `surface` argument registers as the transcript fallback; richer
        # implementations (interactive-terminal, claude-app...) slot in via
        # `surfaces` without the Conductor changing. None: headless.
        self.surfaces: dict[str, SessionSurface] = dict(surfaces or {})
        if surface is not None and "transcript" not in self.surfaces:
            self.surfaces["transcript"] = surface
        self.surface_preference = surface_preference or SurfacePreference()
        self._conductors: dict[str, Conductor] = {}
        # Single-flight guards: concurrent triggers (watchdog, notification
        # click, voice follow-up) share one operation, never spawn duplicates.
        self._recoveries: dict[str, asyncio.Task] = {}
        self._surface_locks: dict[str, asyncio.Lock] = {}
        # Current activity per task ("Running auth tests"), fed by the same
        # event stream everything else consumes - the Manager supervises
        # from state, never by polling raw sessions (spec section 24).
        self._activity: dict[str, str] = {}
        # Outstanding decisions the user owes a worker, kept current from
        # the event stream so every Manager turn can lead with them - an
        # approval is something the Manager acts on, not just a UI state.
        self._attention: dict[str, dict] = {}    # task_id -> {kind, text, id}
        self.approval_policy = approval_policy or ApprovalPolicy()
        # The user's own words behind the manager turns in progress, when
        # they were spoken. send_to_task holds the manager to them. Turns
        # can overlap (a visible Boss takes the next words mid-turn), so
        # what is held to is everything said that is still being worked
        # on, oldest first.
        self._utterances: list[str] = []
        self._utterance = ""
        self._open_turns = 0
        # The SupervisorInbox, when the product wires one: what the Boss
        # reads at the top of a turn and acknowledges at the end of it.
        self.inbox = None
        self.bus.subscribe(self._track_activity)

    def _track_activity(self, event: ObservabilityEvent) -> None:
        if event.task_id is None:
            return
        if event.type in ("runtime.progress", "task.context_updated"):
            summary = event.data.get("summary", "")
            if summary:
                self._activity[event.task_id] = summary[:120]
        elif event.type == "task.approval_required":
            approval = event.data.get("approval") or {}
            self._activity[event.task_id] = \
                f"approval: {event.data.get('question', '')[:100]}"
            self._attention[event.task_id] = {
                "kind": "approval",
                "text": event.data.get("question", "")[:150],
                "approval_id": approval.get("approval_id", "")}
            # Active supervision: an approval is the supervisor's problem
            # the moment it exists, not a UI state waiting to be noticed.
            try:
                asyncio.get_running_loop().create_task(
                    self.supervise_approvals())
            except RuntimeError:
                application_log(
                    "conductor", "approval.supervision_not_scheduled",
                    "approval supervision has no running event loop",
                    severity="debug", task_id=event.task_id)
        elif event.type == "task.needs_input":
            self._activity[event.task_id] = \
                f"question: {event.data.get('question', '')[:100]}"
            self._attention[event.task_id] = {
                "kind": "input",
                "text": event.data.get("question", "")[:150],
                "approval_id": ""}
        elif event.type == "task.completed":
            # A turn that ends on a question is a worker waiting on the
            # user, right now. Until the sweep found it (up to 30 s later)
            # it was in no _attention entry, and a status question asked
            # in between was answered "nothing is waiting on you".
            question = _trailing_question(event.data.get("summary", ""))
            if question:
                self._activity[event.task_id] = f"question: {question[:100]}"
                self._attention[event.task_id] = {
                    "kind": "input", "text": question[:150],
                    "approval_id": ""}
        elif event.type in ("task.approval_resolved", "approval.resolved",
                            "task.resumed", "task.started",
                            "task.message_sent", "task.cancelled",
                            "task.closed"):
            self._attention.pop(event.task_id, None)

    @property
    def manager_busy(self) -> bool:
        """Whether the Manager is mid-turn: a message handed over now
        waits its turn rather than being acted on straight away."""
        return self.manager is not None and bool(
            getattr(self.manager, "busy", False))

    def _emit(self, event_type: str, component: str = "conductor",
              **kwargs) -> None:
        self.bus.emit(ObservabilityEvent(type=event_type,
                                         component=component, **kwargs))

    # -- per-project machinery -------------------------------------------------
    def _conductor(self, project_id: str) -> Conductor:
        if project_id not in self._conductors:
            project = self.projects.get(project_id)
            if project is None:
                raise KeyError(f"no such project: {project_id}")
            store = TaskStore(project.root_path, bus=self.bus,
                              state_dir=self.projects.project_dir(project_id),
                              project_id=project_id)
            self._conductors[project_id] = Conductor(
                project.root_path, self.runtime,
                workspaces=self.workspace_factory(project),
                store=store, bus=self.bus)
        return self._conductors[project_id]

    def _find_task(self, task_id: str) -> tuple[Conductor, Task]:
        """task id -> owning project's conductor, from state alone."""
        for project in self.projects.list():
            conductor = self._conductor(project.id)
            task = conductor.store.get(task_id)
            if task is not None:
                return conductor, task
        raise KeyError(f"no such task: {task_id}")

    def _touch(self, project_id: str, task_id: str | None = None) -> None:
        """Recency and focus are hints. A tool that has done its work must
        not report failure over bookkeeping: measured on 2026-09-01, a
        crash right here (a null focus in global.json) made create_task
        "fail" three times - after each worker was fully created."""
        try:
            self.projects.touch(project_id)
            self.projects.set_focus(project_id=project_id, task_id=task_id)
        except Exception:
            application_log("conductor", "conductor.touch_failed",
                            "recency/focus bookkeeping failed; the tool's "
                            "work stands", severity="warning", exc_info=True,
                            task_id=task_id or "")

    # -- project tools ------------------------------------------------------
    def list_projects(self) -> list[dict]:
        summaries = []
        for project in self.projects.list():
            tasks = self._conductor(project.id).list_tasks()
            active = [t for t in tasks if t.status in
                      ("starting", "running", "waiting_for_user",
                       "interrupted")]
            summaries.append({
                "project_id": project.id,
                "name": project.display_name,
                "status": project.status,
                "active_tasks": len(active),
                "total_tasks": len(tasks)})
        return summaries

    def find_project(self, query: str) -> list[dict]:
        """A small ranked candidate set of bounded metadata - the Manager
        never receives (or walks) the filesystem tree."""
        candidates = self.locator.search(query)
        self._emit("conductor.project_search",
                   data={"query": query,
                         "candidates": [c.to_dict() for c in candidates]})
        return [c.to_dict() for c in candidates]

    def inspect_project(self, project_id: str) -> dict:
        project = self.projects.get(project_id)
        if project is None:
            raise KeyError(f"no such project: {project_id}")
        try:
            context = self.projects.context_path(project_id).read_text()
        except OSError:
            context = ""
        tasks = self._conductor(project_id).list_tasks()
        self._touch(project_id)
        return {"project_id": project.id, "name": project.display_name,
                "path": project.root_path, "status": project.status,
                "context": context[:1500],
                "tasks": [task_summary(t) for t in tasks]}

    def register_project(self, path: str,
                         display_name: str | None = None) -> dict:
        project = self.locator.register(path, display_name)
        self._emit("conductor.project_registered", project_id=project.id,
                   data={"path": project.root_path,
                         "name": project.display_name})
        self._touch(project.id)
        return {"project_id": project.id, "name": project.display_name,
                "path": project.root_path}

    def resolve_project(self, ref: str) -> Project:
        """A project from whatever the user called it: an id, a path, or a
        name. Names go through the locator (registry, then index, then a
        scan), and an unambiguous unregistered winner is registered on the
        way - so create_task takes the name directly instead of costing a
        find_project and a register_project turn first. Ambiguity is an
        error naming the candidates: the model chooses, never this."""
        ref = (ref or "").strip()
        if not ref:
            raise ValueError("which project? give a name or a path")
        known = self.projects.get(ref)
        if known is not None:
            return known
        if "/" in ref or ref.startswith("~"):
            path = Path(ref).expanduser()
            if not path.is_dir():
                raise ValueError(f"{ref} is not a directory")
            return self._register(path)
        candidates = self.locator.search(ref)
        if not candidates:
            raise ValueError(f"no project matching {ref!r}; "
                             "give a path to register it")
        top = candidates[0]
        if len(candidates) > 1 and candidates[1].score >= top.score:
            names = "; ".join(f"{c.name} ({c.path})" for c in candidates[:4])
            raise ValueError(f"{ref!r} matches more than one project: "
                             f"{names} - say which")
        if top.registered_id:
            return self.projects.get(top.registered_id)
        return self._register(top.path)

    def _register(self, path: str | Path) -> Project:
        project = self.locator.register(path)
        self._emit("conductor.project_registered", project_id=project.id,
                   data={"path": project.root_path,
                         "name": project.display_name})
        return project

    # -- task tools -------------------------------------------------------------
    def _surface_request(self, task: Task) -> SurfaceRequest:
        project = self.projects.get(task.project_id)
        cwd = task.workspace.path if task.workspace else \
            (project.root_path if project else ".")
        title = (f"{project.display_name if project else '?'} — "
                 f"{task.title} — "
                 f"{PROVIDER_NAMES.get(task.provider, task.provider)}")
        transcript = getattr(self.runtime, "transcript", None)
        attach = getattr(self.runtime, "attach_handle", None)
        return SurfaceRequest(project_id=task.project_id, task_id=task.id,
                              title=title, working_directory=cwd,
                              provider=task.provider,
                              provider_session_id=task.provider_session_id,
                              transcript_path=str(transcript.path(task.id))
                              if transcript else None,
                              pty_handle=attach(task.id) if attach else None)

    async def executions(self) -> list:
        """The live task -> session -> workspace -> transcript mappings, for
        debugging and invariant checks: at most one execution per task, and
        the session shown in a task's surface is the session doing the work
        (the transcript path in both is the same file)."""
        return await self.runtime.executions()

    async def subagent_for(self, task_id: str):
        """The ManagedSubagent view of one task: the persistent supervised
        worker, its live status, and its structured result. Derived from
        canonical state + runtime health, never a second store."""
        from .subagents import build_subagent
        _, task = self._find_task(task_id)
        health, pending = None, False
        if task.provider_session_id:
            health = await self.runtime.reconcile_session(
                task.provider_session_id)
            pending = bool(await self.runtime.pending_approvals(
                task.provider_session_id))
        recovery = self._recoveries.get(task_id)
        return build_subagent(task, health, pending,
                              recovering=recovery is not None
                              and not recovery.done())

    def _presence(self, task) -> "Presence":
        """One session's place in the user's world, from state we own."""
        from .presence import classify
        finished_ago = None
        if task.status in ("completed", "failed", "cancelled"):
            stamp = task.updated_at or task.created_at
            try:
                then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                finished_ago = (datetime.now(timezone.utc)
                                - then).total_seconds()
            except Exception:
                finished_ago = None
                application_log("conductor", "presence.timestamp_invalid",
                                "could not parse task timestamp",
                                severity="warning", task_id=task.id,
                                stamp=str(stamp))
        return classify(task.status,
                        hidden_by_user=bool(getattr(task, "hidden", False)),
                        seconds_since_finished=finished_ago,
                        has_provider_session=bool(task.provider_session_id))

    async def list_open_sessions(self) -> list[dict]:
        """The sessions the user currently has open.

        Not every resumable provider session, and not every task on disk: a
        session is open when it has a live card and its work is unfinished.
        The tray renders from the same classification, so what the Manager
        believes is open and what the user sees cannot drift.

        A task whose worker has died is corrected by sweep_stuck rather
        than filtered here: hiding it would leave the store still claiming
        it runs. The sweep marks it interrupted, so it reads as what it is
        - unfinished and resumable - instead of live for ever.
        """
        from .manager import stack_positions
        out = []
        tasks = self.list_tasks()
        # The stack position has to travel with the tool result, not just
        # sit in the prompt's registry: asked for "the one you started
        # last" the Manager calls this, reads a list with no positions in
        # it, falls back to timestamps that all say "just now", and gives
        # up. Same fact, both places it might look.
        positions = stack_positions(tasks)
        for task in tasks:
            presence = self._presence(task)
            if not presence.is_open:
                continue
            row = {"subagent_id": f"sub_{task.id}", "task_id": task.id,
                   "project_id": task.project_id, "title": task.title,
                   "state": task.status, "visible": True,
                   "activity": self._activity.get(task.id, "")}
            nth = positions.get(task.id, (0, 0))[0]
            if nth:
                total = positions[task.id][1]
                row["on_screen"] = f"{nth} of {total}"
                row["is_newest"] = nth == total
                row["is_oldest"] = nth == 1
            out.append((nth, row))
        # Stack order, oldest first - the order the user sees and counts in.
        # Sorting on the rendered "on_screen" string put 10 before 2.
        out.sort(key=lambda pair: pair[0])
        return [row for _, row in out]

    # -- the Boss session ----------------------------------------------------
    # Set by the product when the Boss is a visible session; None means the
    # invisible SDK Boss, which keeps no record of its own.
    boss_store = None

    @property
    def current_boss_session_id(self) -> str | None:
        """Canonical: from the conversation binding on disk, never from a
        window, a model request or the last terminal opened."""
        if self.boss_store is None:
            return None
        current = self.boss_store.current_boss()
        return current.id if current else None

    def note_for_voice(self, text: str) -> str:
        """Something the voice should know and the user should not hear.

        The Boss's spoken answer is all the voice used to get; everything
        the Boss learned on the way - which file, which task id, which PR
        number, what not to promise - was either read aloud or lost. Then
        "which one was the draft?" meant another delegation for a fact
        the Boss had ten seconds earlier. Notes ride back with the turn on
        the commentary channel: the voice keeps them, answers follow-ups
        from them, and never reads them out. Nothing here is typed into
        any window.
        """
        text = " ".join(str(text or "").split())
        if not text:
            return "nothing noted"
        if not self._open_turns:
            # Made between turns - in reply to a pushed worker update,
            # mostly. A turn's notes ride back with the turn; there is
            # no turn here to ride, and the next one starts clean, so
            # this used to be dropped on the floor. Measured 08:44:43:
            # the PR 59 summary the user had asked for went into a note
            # like this, and they heard nothing until they said hello
            # again. It goes to the voice now (conduct.notes_between_turns).
            self._emit("boss.note_for_voice", "manager",
                       data={"text": text[:1200], "outside_turn": True})
            return "noted for the voice"
        notes = getattr(self, "_voice_notes", None)
        if notes is None:
            self._voice_notes = notes = []
        notes.append(text[:1200])
        self._emit("boss.note_for_voice", "manager",
                   data={"text": text[:300]})
        return "noted for the voice"

    # What the voice model said aloud, newest last. The voice answers
    # greetings and fills gaps on its own, so the user has heard things
    # the Boss never said; this is how the Boss finds out. Nothing here
    # is typed into the Boss's window - "Voice said: hi there" between
    # the user's own lines was the alternative, and the window is the
    # transcript the user reads.
    VOICE_SAID_KEEP = 12

    def voice_spoke(self, text: str) -> None:
        text = " ".join(str(text or "").split())
        if not text:
            return
        said = getattr(self, "_voice_said", None)
        if said is None:
            self._voice_said = said = []
        said.append((time.time(), text[:600]))
        del said[:-self.VOICE_SAID_KEEP]
        record = getattr(self.manager, "record_voice_said", None)
        if record is not None:
            record(text)

    def tell_user(self, text: str) -> str:
        """Something the user should hear now, in the Boss's words.

        The spoken counterpart of note_for_voice. Workers report to the
        Boss, not to the user: their turn ends arrive in its window as
        worker updates, and a mechanical announcer used to read every one
        of them out - "the X agent finished" for a turn that ended mid-
        work, three times for three workers - before anyone had judged
        it. Now the announcer is limited to what blocks the user, and a
        finish reaches them only when the Boss decides it should, in the
        words it chooses.

        During a voice turn the Boss's reply is what the user hears, so
        this refuses rather than speaking twice.
        """
        text = " ".join(str(text or "").split())
        if not text:
            return "nothing to say"
        manager = self.manager
        answering = getattr(manager, "_in_voice_turn", None)
        if answering is None:
            answering = bool(getattr(manager, "busy", False))
        if answering:
            return ("you are answering the user right now; put this in "
                    "your reply instead")
        self._emit("boss.tell_user", "manager", data={"text": text[:600]})
        if hasattr(manager, "_told_user_in_push"):
            manager._told_user_in_push = True    # its reply need not repeat it
        return "said"

    def what_the_voice_said(self) -> str:
        """The last things the voice said aloud, with their age."""
        said = getattr(self, "_voice_said", None) or []
        if not said:
            return "The voice has said nothing yet."
        now = time.time()
        return "\n".join(f"{_age(now - at)}: {text}" for at, text in said)

    def new_conversation(self) -> str:
        """"New voice chat": the next turn opens a fresh Boss."""
        if self.boss_store is None:
            raise RuntimeError("no Boss session store is configured")
        return self.boss_store.new_conversation()

    def set_parent_boss(self, task_id: str, boss_session_id: str) -> None:
        """Record which Boss started this worker - explicit, persisted,
        never inferred from timing, project or a window."""
        conductor, task = self._find_task(task_id)
        state = conductor._subagent_state(task)
        if state.parent_boss_session_id == boss_session_id:
            return
        state.parent_boss_session_id = boss_session_id
        conductor.subagents.save(state)
        self._emit("subagent.parent_set", "task", task_id=task.id,
                   data={"boss_session_id": boss_session_id})

    def dismiss_card(self, task_id: str) -> dict | None:
        """The user waved a worker's card away. See Conductor.dismiss_card."""
        try:
            conductor, task = self._find_task(task_id)
        except Exception:
            return None
        return conductor.dismiss_card(task.id)

    def children_of(self, boss_session_id: str) -> list[dict]:
        """The Boss's workers, from their own persisted parent field."""
        out = []
        for task in self.list_tasks():
            state = self._conductor(task.project_id).subagent_state_dict(task.id)
            if state and state.get("parent_boss_session_id") == boss_session_id:
                out.append(state)
        return out

    def subagent_states(self) -> list[dict]:
        """Canonical state for every task the user can see, for rendering
        the tray from truth - on startup, on UI reconnect - rather than
        from a replay of notifications."""
        out = []
        for task in self.list_tasks():
            if not self._presence(task).is_visible:
                continue
            state = self._conductor(task.project_id).subagent_state_dict(task.id)
            if state is not None:
                out.append(state)
        # In the order the workers were started, across projects: the
        # tray is chronological, and a replay in store order put the
        # cards back in a different order than the user last saw them.
        out.sort(key=lambda state: state.get("created_at") or "")
        return out

    async def list_recent_sessions(self) -> list[dict]:
        """Finished sessions still on screen. Visible, but not open."""
        from .presence import VISIBLE_RECENT
        return [{"subagent_id": f"sub_{t.id}", "task_id": t.id,
                 "project_id": t.project_id, "title": t.title,
                 "state": t.status}
                for t in self.list_tasks()
                if self._presence(t).display == VISIBLE_RECENT]

    def search_sessions(self, query: str = "", limit: int = 20) -> list[dict]:
        """Historical work, resumable but not open. This is where a session
        the user remembers from yesterday is found."""
        needle = " ".join(query.lower().split())
        found = []
        for task in self.list_tasks():
            presence = self._presence(task)
            if presence.is_open:
                continue           # open sessions belong to the other tool
            hay = f"{task.title} {task.goal}".lower()
            if needle and needle not in hay:
                continue
            found.append({"task_id": task.id, "project_id": task.project_id,
                          "title": task.title, "state": task.status,
                          "resumable": presence.resumable,
                          "last_active": idle_for(task)})
        return found[:limit]

    def _worker_gone(self, task_id: str, report: dict, window: str) -> None:
        """A running task with no worker behind it is interrupted: back
        within reach of resume_task, and no longer a busy worker."""
        conductor, _ = self._find_task(task_id)
        conductor.store.update(task_id, status="interrupted")
        self._emit("task.worker_gone", "task", task_id=task_id,
                   severity="warning",
                   data={"was": "running", "window": window})
        report["workers_gone"].append(task_id)

    async def _process_there(self, task, name: str) -> bool:
        """Before a listed worker is believed: is its process running?

        Measured 2026-08-30 (conductor-94883): cond_task_1c2db9f0's
        claude exited during a restart at 09:09:25Z, its cmux workspace
        stayed listed as a bare shell, and the task sat "running" and
        busy for forty minutes - one of three worker slots, so every
        create_task after it was refused with "3 workers are busy", and
        each send_to_task to it timed out after 45 s. A runtime that
        cannot see processes, or a check that fails, leaves the worker
        alone: only a clear "no process" ends it.
        """
        runtime = self._worker_runtime(task)
        ask = getattr(runtime, "worker_process_alive", None)
        if ask is None:
            return True
        cwd = task.workspace.path if task.workspace else None
        try:
            return bool(await asyncio.to_thread(
                ask, name, cwd, task.provider_session_id))
        except Exception as exc:
            self._emit("runtime.session_check_failed", "runtime",
                       severity="warning", task_id=task.id,
                       data={"session": name, "error": str(exc)[:200]})
            return True

    def _worker_runtime(self, task):
        """The runtime hosting this task's worker - for a question whose
        answer depends on which binary to look for. The router sends an
        unrouted call to its first runtime, and asking Claude's runtime
        whether a Codex worker's process is alive would say no, every
        time, and end a live worker. A provider nothing here hosts gets
        no runtime, and so no opinion."""
        runtimes = getattr(self.runtime, "runtimes", None)
        if not isinstance(runtimes, dict):
            return self.runtime
        if task.provider in runtimes:
            return runtimes[task.provider]
        if task.provider == "claude-code":
            where = self.runtime.location_of(task.provider_session_id or "")
            return runtimes.get(where) or runtimes.get("local")
        return None

    async def _process_vouches(self, task, name: str) -> bool:
        """Before a worker with no window is declared gone: is its
        process running?

        Measured 2026-08-31 (conductor-22938): a relaunch's first sweep
        ran before the restarted host listed anything, has-session named
        nobody, and a worker whose claude was alive the whole time was
        marked interrupted (`window=gone`) - the user had to ask for a
        resume by voice. The process table does not restart with the
        host, so ask it. Only a clear "yes" vouches: a runtime without
        the hook, a missing checkout or a failed check leaves both host
        accounts standing.
        """
        runtime = self._worker_runtime(task)
        ask = getattr(runtime, "worker_process_alive", None)
        cwd = task.workspace.path if task.workspace else None
        if ask is None or not cwd:
            return False
        try:
            alive = bool(await asyncio.to_thread(
                ask, name, cwd, task.provider_session_id))
        except Exception as exc:
            self._emit("runtime.session_check_failed", "runtime",
                       severity="warning", task_id=task.id,
                       data={"session": name, "error": str(exc)[:200]})
            return False
        if alive:
            self._emit("runtime.session_process_alive", "runtime",
                       severity="warning", task_id=task.id,
                       data={"session": name, "window": "gone"})
        return alive

    async def _still_there(self, task_id: str, expected: set,
                           listed: set) -> bool:
        """Before a running worker is declared gone: ask about it by name.

        Measured live: cmux 0.64 changed its listing's columns, the parser
        read every title as empty, and the sweep - trusting an answer that
        named nobody - interrupted every worker thirty seconds after it
        started. has-session, asked by name, said each was alive the whole
        time. The listing decides who to ask about; it does not get the
        last word.
        """
        ask = getattr(self.runtime, "session_alive", None)
        if ask is None:
            return False
        for name in sorted(expected):
            try:
                alive = await asyncio.to_thread(ask, name)
            except Exception as exc:
                self._emit("runtime.session_check_failed", "runtime",
                           task_id=task_id, severity="warning",
                           data={"name": name, "error": str(exc)[:200]})
                continue
            if alive:
                self._emit("runtime.session_list_disagreed", "runtime",
                           task_id=task_id, severity="warning",
                           data={"alive": name,
                                 "listed": sorted(listed)[:12]})
                return True
        return False

    async def watch_unwatched(self) -> list[str]:
        """A worker running with nobody reading its transcript gets a
        watcher. Its turns produce no events otherwise, so a message the
        user typed into its own window finishes and the card never moves
        - the work is done and the UI still says "Working".

        Run by the sweep, and once at startup: launching wakes nothing,
        so after a restart every live worker was unread until the first
        sweep, thirty seconds in, and an adopted watcher starts at the
        end of the transcript - a finish inside that window was never
        seen by anyone. We restart after every merge.

        The async form probes off the loop and starts its watcher ON it
        - a watcher is an asyncio task, and creating one in a thread
        raised "no running event loop" every sweep, so no adopted worker
        was watched at all. A runtime with only the sync form keeps
        going to a thread: its probe must not stall the loop.
        """
        watch_async = getattr(self.runtime, "ensure_watched_async", None)
        watch = getattr(self.runtime, "ensure_watched", None)
        if watch_async is None and watch is None:
            return []
        watched: list[str] = []
        for task in self.list_tasks():
            if task.status not in ("running", "working", "starting"):
                continue
            if not task.provider_session_id:
                continue
            cwd = task.workspace.path if task.workspace else None
            if not cwd:
                continue
            try:
                if watch_async is not None:
                    started = await watch_async(task.provider_session_id, cwd)
                else:
                    started = await asyncio.to_thread(
                        watch, task.provider_session_id, cwd)
                if started:
                    watched.append(task.id)
                    # A watcher without a subscriber is a reader with no
                    # one to tell: adoption creates a fresh session whose
                    # handler list is empty, and the Conductor's handler -
                    # the one that reduces events into the card and the
                    # store - only attaches on create/resume. Attach it
                    # here, and only then deliver a finish the worker
                    # reached while nobody was reading (the adopted
                    # watcher starts at the end of the transcript, so
                    # that finish would otherwise never produce an event
                    # and the task would say "running" for ever).
                    conductor = self._conductor(task.project_id)
                    await conductor._subscribe(conductor.store.get(task.id))
                    deliver = getattr(self.runtime,
                                      "deliver_adopted_finish", None)
                    recovered = bool(deliver(task.provider_session_id)) \
                        if deliver is not None else False
                    self._emit("task.watch_resumed", "task",
                               task_id=task.id,
                               data={"reason": "nobody was reading it",
                                     "finish_recovered": recovered})
            except Exception as exc:
                self._emit("task.watch_failed", "task", task_id=task.id,
                           severity="warning",
                           data={"error": str(exc)[:200]})
        return watched

    async def sweep_stuck(self) -> dict:
        """Find and clear anything wedged. Runs on a timer, not on hope.

        Two ways a session gets stuck, and they need opposite treatment:

        A worker held at a boot dialog is alive and fixable - answer it.

        A task the store calls "running" whose PTY is gone is not fixable
        and not running; it is interrupted. Saying so puts it back within
        reach of resume_task instead of leaving it counted as live for ever.
        Only "running" qualifies: a dormant task legitimately has no PTY,
        because startup no longer wakes anything.
        """
        report = {"dialogs_cleared": [], "workers_gone": [],
                  "waiting_on_you": [], "retired": [], "panes_closed": []}
        unstick = getattr(self.runtime, "unstick", None)
        if unstick is not None:
            try:
                report["dialogs_cleared"] = await unstick()
            except Exception as exc:
                self._emit("runtime.sweep_failed", "runtime",
                           severity="warning", data={"error": str(exc)[:200]})
        live = getattr(self.runtime, "live_session_names", None)
        if live is not None:
            try:
                names = await asyncio.to_thread(live)
            except Exception as exc:
                names = None
                self._emit("runtime.session_list_failed", "runtime",
                           severity="warning",
                           data={"error": str(exc)[:200]})
            if names is not None:
                for task in self.list_tasks():
                    if task.status != "running":
                        continue
                    expected = {f"cond_task_{task.id}",
                                f"cond_{task.id}"}
                    if task.provider_session_id:
                        expected.add(
                            f"cond_resumed_{task.provider_session_id[:8]}")
                    listed = expected & names
                    if listed:
                        # The window is there. Is the worker? A window
                        # can outlive its process (an empty shell in a
                        # cmux workspace), and one counted as busy for
                        # ever holds a worker slot nobody can use.
                        if await self._process_there(task, min(listed)):
                            continue
                        self._worker_gone(task.id, report, window="empty")
                        continue
                    if await self._still_there(task.id, expected, names):
                        continue
                    if await self._process_vouches(task, min(expected)):
                        continue
                    self._worker_gone(task.id, report, window="gone")
        report["gui_lease"] = await asyncio.to_thread(self.publish_gui_lease)
        report["retired"] = await self._retire_idle()
        report["panes_closed"] = await self._close_finished_panes()
        report["watched"] = await self.watch_unwatched()
        # A Task still `running` whose sidecar is terminal: the stuck
        # pair a lost running-edge leaves behind (a retire-and-revive in
        # one process, the restart starting the next with the pair
        # already inconsistent). The worker is muted and the card says
        # Running for ever; the event handler heals it when the worker
        # next speaks, the sweep when it does not.
        report["reconciled"] = []
        for task in self.list_tasks():
            if task.status != "running":
                continue
            try:
                conductor, _ = self._find_task(task.id)
                if await conductor.reconcile_stuck_pair(task.id):
                    report["reconciled"].append(task.id)
                    self._emit("task.stuck_pair_reconciled", "task",
                               task_id=task.id,
                               provider_session_id=task.provider_session_id)
            except Exception as exc:
                self._emit("task.reconcile_failed", "task", task_id=task.id,
                           severity="warning",
                           data={"error": str(exc)[:200]})
        # A worker sitting on a decision nothing is watching. The card
        # never appeared because the watcher that would have raised it does
        # not exist - after a restart, backgrounded workers have none. Raise
        # it here instead; the sweep is the only thing still looking.
        prompts = getattr(self.runtime, "unwatched_prompts", None)
        if prompts is not None:
            try:
                # One screen read per live window, each a subprocess:
                # measured at 6-7 s for twenty windows, and it ran ON the
                # loop, so every 30 s sweep froze keys, audio and the
                # session for that long (app.loop_stalled, 62 times in one
                # run). Off the loop, the sweep costs the user nothing.
                waiting = await asyncio.to_thread(prompts)
            except Exception:
                waiting = []
            for item in waiting:
                task_id = item["session"].replace("cond_task_", "").replace(
                    "cond_", "")
                match = next((t for t in self.list_tasks()
                              if t.id.endswith(task_id)
                              or task_id.endswith(t.id)), None)
                if match is None:
                    continue
                if self._attention.get(match.id):
                    continue          # already on screen; do not repeat it
                self._emit("task.needs_input", "task", task_id=match.id,
                           data={"question": item["question"],
                                 "found_by": "sweep"})
                report["waiting_on_you"].append(match.id)
        if report["dialogs_cleared"] or report["workers_gone"] \
                or report["waiting_on_you"] or report["reconciled"]:
            self._emit("conductor.sweep", "conductor", data=report)
        return report

    def gui_tasks(self) -> list[str]:
        """The tasks that may drive this machine's GUI right now.

        Computer use, and running: a task that has finished, failed or
        been interrupted has no business posting a click, and neither has
        one this conductor is not supervising at all.
        """
        return [task.id for task in self.list_tasks()
                if getattr(task, "computer", False)
                and task.status in ACTIVE_GUI_STATUSES]

    def publish_gui_lease(self) -> list[str]:
        """Write the live roster into the instance lock, so a worker can
        ask whether it is still held before it touches the keyboard.

        This is also the heartbeat: a conductor whose loop has stopped
        turning stops renewing it, and its workers stop typing. On
        2026-09-08 one of five workers left behind by a restart was still
        driving Chrome with nothing supervising it.
        """
        allowed = self.gui_tasks()
        try:
            if not instance.heartbeat(self.projects.home, current_run(),
                                      allowed):
                self._emit("conductor.lease_not_ours", "conductor",
                           severity="warning",
                           data={"reason": "the instance lock belongs to "
                                           "another conductor"})
        except Exception as exc:
            # Never silently: a lease that stopped being written is a
            # worker that will stop being able to act, and the reason for
            # it has to be findable.
            application_log("conductor", "conductor.lease_write_failed",
                            "could not write the GUI lease",
                            severity="error", exc_info=True)
            self._emit("conductor.lease_write_failed", "conductor",
                       severity="warning", data={"error": str(exc)[:200]})
        return allowed

    RETIRABLE = ("waiting_for_user", "interrupted")

    def _idle_seconds(self, task: Task) -> float | None:
        """How long since anyone addressed this task: the later of its last
        state change and the last instruction the user sent it."""
        latest = None
        for stamp in (task.updated_at, task.last_instruction_at,
                      task.created_at):
            if not stamp:
                continue
            try:
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                application_log("conductor", "presence.timestamp_invalid",
                                "could not parse task timestamp",
                                severity="warning", task_id=task.id,
                                stamp=str(stamp))
                continue
            if latest is None or when > latest:
                latest = when
        if latest is None:
            return None
        return (datetime.now(timezone.utc) - latest).total_seconds()

    async def _retire_idle(self) -> list[str]:
        """Close the tasks nobody has addressed for idle_retire_s.

        A PTY worker ends a turn, not a task: after it answers, the task
        is waiting_for_user, and one whose worker died is interrupted.
        Neither is terminal, so neither ever left the tray - 29 cards
        accumulated in a day and a half, most from sessions whose windows
        were long gone. The only thing that closed a task was someone
        saying so.

        Age is the rule, and it is deterministic: a task unaddressed for
        this long is closed the way the user would close it -
        complete_task when it has a result, cancel_task when it does not -
        so it ends its session, keeps its branch and context, and gives
        back a clean directory (a dirty one is kept and reported, as
        always). The session stays resumable; retired is history, not
        deletion. Anything the user addresses resets the clock.
        """
        if not self.idle_retire_s:
            return []
        retired: list[str] = []
        for task in self.list_tasks():
            if task.status not in self.RETIRABLE:
                continue
            idle = self._idle_seconds(task)
            if idle is None or idle < self.idle_retire_s:
                continue
            conductor, _ = self._find_task(task.id)
            closed_as = "completed" if task.result else "cancelled"
            close = (conductor.complete_task if task.result
                     else conductor.cancel_task)
            try:
                await close(task.id)
                if closed_as == "completed":
                    # Retired for sitting unread this long: nobody is
                    # coming to read the pane either.
                    await conductor.close_pane(task.id, "retired")
            except Exception as exc:
                self._emit("task.retire_failed", "task", task_id=task.id,
                           project_id=task.project_id, severity="warning",
                           data={"was": task.status,
                                 "error": str(exc)[:200]})
                continue
            self._emit("task.retired", "task", task_id=task.id,
                       project_id=task.project_id,
                       provider_session_id=task.provider_session_id,
                       data={"was": task.status, "closed_as": closed_as,
                             "idle_s": round(idle),
                             "after_s": self.idle_retire_s})
            retired.append(task.id)
        return retired

    async def _close_finished_panes(self) -> list[str]:
        """Completion leaves a worker's pane open to be read. Once a
        completed task has sat unaddressed for the idle-retire window,
        its session is ended and its directory given back."""
        if not self.idle_retire_s:
            return []
        closed: list[str] = []
        for task in self.list_tasks():
            if task.status != "completed" or not task.provider_session_id:
                continue
            idle = self._idle_seconds(task)
            if idle is None or idle < self.idle_retire_s:
                continue
            try:
                status = await self.runtime.get_status(task.provider_session_id)
            except Exception:
                status = "disconnected"
            if status in (None, "disconnected"):
                continue                   # nothing left to close
            conductor, _ = self._find_task(task.id)
            try:
                await conductor.close_pane(task.id, "finished")
            except Exception as exc:
                self._emit("task.pane_close_failed", "task", task_id=task.id,
                           severity="warning", data={"error": str(exc)[:200]})
                continue
            closed.append(task.id)
        return closed

    async def situation(self) -> str:
        """One spoken paragraph covering everything in flight.

        Composed here rather than left to the Manager so the answer to
        "what's going on?" has the same shape every time and leads with
        whatever is blocked on the user.
        """
        from .situation import situation_report
        return situation_report(await self.list_open_sessions(),
                                await self.list_recent_sessions())

    def session_counts(self) -> dict:
        """What the Manager answers counting questions from."""
        from .presence import roster
        return roster([self._presence(t) for t in self.list_tasks()])

    async def list_subagents(self) -> list[dict]:
        """The supervisor's answer to "what is everything doing?": every
        worker's status and current activity, attention first, from state -
        one blocked subagent never hides or blocks the others."""
        views = await self.subagents()
        out = []
        for view in views:
            entry = {"subagent_id": view.id, "task_id": view.task_id,
                     "title": view.title, "project_id": view.project_id,
                     "status": view.status}
            activity = self._activity.get(view.task_id)
            if activity and view.status not in ("completed", "failed",
                                                "cancelled"):
                entry["activity"] = activity
            if view.status == "waiting_for_approval" and \
                    view.provider_session_id:
                pending = await self.runtime.pending_approvals(
                    view.provider_session_id)
                if pending:
                    entry["pending_approvals"] = pending
            if view.result is not None:
                entry["result_summary"] = view.result.summary[:150]
            out.append(entry)
        return out

    async def subagents(self) -> list:
        """Every managed subagent across projects, active ones first.

        Probed together, not in turn: each view asks the runtime about
        one session, and with nineteen workers that was nineteen probes
        end to end. The Boss asks this for "what is everything doing?" -
        measured at 14.5s before, dominated by the per-task provider
        listing the runtime now shares (TmuxClaudeRuntime._feed).
        """
        views = list(await asyncio.gather(
            *(self.subagent_for(task.id) for task in self.list_tasks())))
        order = {"waiting_for_approval": 0, "waiting_for_input": 1,
                 "working": 2, "starting": 3, "interrupted": 4}
        views.sort(key=lambda s: order.get(s.status, 9))
        return views

    def _surface_candidates(self, task: Task) -> list[tuple[str,
                                                            SessionSurface]]:
        """Registered implementations in the provider's preference order,
        remainder appended - attach falls through until one works. A choice
        of surface is only ever a different view of the same execution."""
        ordered = [(name, self.surfaces[name])
                   for name in self.surface_preference.order_for(task.provider)
                   if name in self.surfaces]
        ordered += [(name, impl) for name, impl in self.surfaces.items()
                    if all(name != n for n, _ in ordered)]
        return ordered

    def _surface_impl(self, handle: SurfaceHandle | None) -> SessionSurface \
            | None:
        """The implementation that owns a stored handle."""
        if handle is not None:
            name = handle.metadata.get("surface_name")
            if name in self.surfaces:
                return self.surfaces[name]
        picked = None if not self.surfaces else \
            next(iter(self.surfaces.values()))
        return picked

    @property
    def surface(self) -> SessionSurface | None:
        """Compatibility view: the fallback/only registered surface."""
        if not self.surfaces:
            return None
        return self.surfaces.get("transcript",
                                 next(iter(self.surfaces.values())))

    async def _open_surface(self, task: Task,
                            reason: str) -> SurfaceHandle | None:
        """Best effort: a surface failing must never fail the task. Guarded
        per task so two simultaneous requests yield one terminal, and always
        idempotent: an existing healthy surface is returned, not duplicated.
        reason is mandatory (spec section 35) - no surface appears without a
        recorded why."""
        candidates = self._surface_candidates(task)
        if not candidates or (task.surface_mode == "background" and
                              reason == "new_task"):
            return None
        lock = self._surface_locks.setdefault(task.id, asyncio.Lock())
        async with lock:
            fresh = self._conductor(task.project_id).store.get(task.id)
            if fresh and fresh.surface:
                handle = SurfaceHandle.from_dict(fresh.surface)
                owner = self._surface_impl(handle)
                if owner is not None and await asyncio.to_thread(owner.is_available, handle):
                    return handle    # reuse, never duplicate
            self._emit("surface.create_started", task_id=task.id,
                       project_id=task.project_id, data={"reason": reason})
            request = self._surface_request(fresh or task)
            handle, errors = None, []
            for name, impl in candidates:
                try:
                    handle = await asyncio.to_thread(impl.attach, request)
                    handle.metadata.setdefault("surface_name", name)
                    handle.metadata.setdefault("interactive",
                                               str(impl.interactive).lower())
                    break
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
            if handle is None:
                self._emit("surface.missing", task_id=task.id,
                           severity="warning",
                           data={"errors": errors[:3], "reason": reason})
                return None
            self._conductor(task.project_id).store.update(
                task.id, surface=handle.to_dict())
            self._emit("surface.created", task_id=task.id,
                       project_id=task.project_id,
                       data={"type": handle.type, "reason": reason,
                             "surface": handle.metadata.get("surface_name"),
                             "window": handle.native_window_id})
            return handle

    # Worker statuses that hold a slot under the concurrency cap. A task
    # stays "running" from its start until someone says complete or
    # cancel - hours after its worker last did anything. Measured: every
    # launch for twenty minutes was refused, "3 tasks are already
    # running", while all three slots were held by an answered question
    # from two hours before, a task whose PR was already merged, and a
    # worker whose window was gone. The cap is on workers doing work.
    # Idle, or waiting on the user, is not work.
    BUSY_WORKER = ("starting", "working", "recovering")

    def _worker_busy(self, task) -> bool:
        try:
            state = self._conductor(task.project_id).subagent_state_dict(task.id)
        except Exception:
            return True                    # unknown counts, not free
        if state is None:
            return True                    # not even started: on its way
        return state.get("status") in self.BUSY_WORKER

    async def create_task(self, title: str, goal: str,
                          project_id: str | None = None,
                          background: bool = False,
                          location: str | None = None,
                          project: str | None = None,
                          computer: bool = False,
                          provider: str | None = None) -> Task:
        """location picks where this ONE worker runs - "local" or "cloud" -
        and is ignored by runtimes that only offer one place. It is a real
        difference in what the worker can do, not a preference: a cloud
        worker gets a copy of the tree and reports nothing unless asked, a
        local one sees uncommitted work and is supervised throughout.

        project is the project as the user said it - a name or a path -
        resolved (and registered on first use) right here, so the routine
        case is one tool call rather than find/register/create."""
        if project_id is None and project:
            project_id = self.resolve_project(project).id
        if project_id is None:
            # Deterministic, not semantic: only a lone registered project
            # may be implied. Anything else must be scoped explicitly.
            projects = self.projects.list()
            if len(projects) != 1:
                raise ValueError("create_task needs a project_id when more "
                                 "than one project is registered")
            project_id = projects[0].id
        busy = [t for t in self.list_tasks()
                if t.status in ("starting", "running") and self._worker_busy(t)]
        if len(busy) >= self.max_concurrent_tasks:
            raise RuntimeError(
                f"{len(busy)} workers are busy (limit "
                f"{self.max_concurrent_tasks}); wait for one to finish its "
                "turn, or pause or cancel one")
        project = self.locator.refresh(project_id)
        if project.status != "available":
            raise RuntimeError(f"project {project.display_name} is "
                               f"{project.status}; resolve its path first")
        if computer:
            can, why = computer_state()
            if not can:
                raise RuntimeError(
                    f"cannot start a computer-use worker yet: {why}")
        conductor = self._conductor(project_id)
        task = await conductor.create_task(title, goal, location=location,
                                           computer=computer,
                                           provider=provider or None)
        if background:
            task = conductor.store.update(task.id, surface_mode="background")
        else:
            await self._open_surface(task, reason="new_task")
            task = conductor.store.get(task.id)
        self._touch(project_id, task.id)
        if computer:
            # Its lease has to exist before its first click, and the
            # sweep is up to thirty seconds away.
            await asyncio.to_thread(self.publish_gui_lease)
        return task

    async def focus_task(self, task_id: str,
                         project_id: str | None = None) -> None:
        """Bring a task's visible surface forward - a pure UI operation; the
        worker never receives a message. A closed window is recovered by
        opening a fresh surface onto the same provider session; a background
        task becomes visible on demand (spec sections 18, 22-23)."""
        conductor, task = self._find_task(task_id)
        self._emit("surface.focus_requested", task_id=task.id,
                   project_id=task.project_id)
        # WHERE the worker runs decides how to show it. Asking which
        # capabilities the runtime happens to offer was wrong twice over:
        # a local session answers None to is_readable - which means "no",
        # not "not readable in the cloud" - so `not readable(...)` was
        # true and the cloud branch ran for local workers; and a session
        # the routing map has never heard of (every session after a
        # restart, because that map lives in memory) dispatched to the
        # first runtime that HAD the method, which is the cloud one. Both
        # paths ended in a cloud reveal for a worker sitting in a cmux
        # workspace, so the notification marked itself opened and the user
        # was taken nowhere.
        location = None
        locate = getattr(self.runtime, "location_of", None)
        if callable(locate) and task.provider_session_id:
            try:
                answer = locate(task.provider_session_id)
                location = answer if isinstance(answer, str) else None
            except Exception:
                location = None
        show = getattr(self.runtime, "show", None)
        readable = getattr(self.runtime, "is_readable", None)
        # A cloud worker has no window here to raise. Tapping its card
        # should put the session in front of the user, which means its own
        # page - the Claude app when that can show one, the web otherwise.
        # Not a teleport: that spends a minute building a local pane, and
        # the user asked to look at it, not to move it.
        # A runtime that does not route has no opinion on where this
        # session lives, and one that does is believed: only "cloud" takes
        # the cloud path. Unrouted runtimes fall back to the capability
        # question, which is the behaviour that existed before routing.
        elsewhere = location == "cloud" if location is not None else True
        if elsewhere and show is not None \
                and task.provider_session_id \
                and (readable is None
                     or not readable(task.provider_session_id)):
            try:
                where = show(task.provider_session_id)
                if where:
                    self._emit("task.revealed", "task", task_id=task.id,
                               data={"where": where})
                    return
                # Revealed nowhere is not revealed. Fall through to the
                # surface rather than reporting success for a click that
                # put nothing on screen.
                self._emit("surface.reveal_failed", "task", task_id=task.id,
                           severity="warning",
                           data={"error": "nothing to show"})
            except Exception as exc:
                self._emit("surface.reveal_failed", "task", task_id=task.id,
                           severity="warning", data={"error": str(exc)[:200]})
        # Startup leaves workers asleep, so showing one means waking it first;
        # a surface onto a dead session would render an empty window.
        if task.provider_session_id:
            try:
                await conductor._ensure_session(task)
            except Exception as exc:
                self._emit("runtime.session_missing", task_id=task.id,
                           severity="warning", data={"error": str(exc)[:200]})
        if not self.surfaces:
            raise RuntimeError("no session surface is configured")
        handle = SurfaceHandle.from_dict(task.surface) if task.surface \
            else None
        # Wrong-surface protection: navigate by ids, never by title.
        if handle and handle.metadata.get("task_id") not in ("", task.id):
            handle = None
        impl = self._surface_impl(handle)
        if handle and impl is not None and await asyncio.to_thread(impl.is_available, handle):
            await asyncio.to_thread(impl.focus, handle)
            self._emit("surface.focused", task_id=task.id,
                       project_id=task.project_id)
        else:
            # The user explicitly asked for visibility: the one case where a
            # missing surface is recreated (spec section 13, case B).
            conductor.store.update(task.id, surface=None,
                                   surface_mode="visible")
            task = conductor.store.get(task.id)
            new_handle = await self._open_surface(
                task, reason="user_requested_visibility")
            if new_handle is None:
                self._emit("surface.recovery_failed", task_id=task.id,
                           severity="error")
                raise RuntimeError(f"could not open a surface for "
                                   f"{task.title}")
            # Opening is not showing. A Terminal window arrives in front
            # of you, so this was invisible for years; a cmux workspace is
            # attached to WITHOUT being selected, so the click opened the
            # right session behind whatever the user was looking at and
            # read as nothing having happened.
            fresh_impl = self._surface_impl(new_handle)
            if fresh_impl is not None:
                try:
                    await asyncio.to_thread(fresh_impl.focus, new_handle)
                except Exception as exc:
                    self._emit("surface.focus_failed", task_id=task.id,
                               severity="warning",
                               data={"error": str(exc)[:200]})
            self._emit("surface.recovered", task_id=task.id,
                       project_id=task.project_id,
                       data={"window": new_handle.native_window_id})
        self._touch(task.project_id, task.id)

    def list_tasks(self, project_id: str | None = None) -> list[Task]:
        if project_id is not None:
            return self._conductor(project_id).list_tasks()
        tasks = []
        for project in self.projects.list():
            tasks.extend(self._conductor(project.id).list_tasks())
        return tasks

    def _placed_elsewhere(self, session_id: str) -> bool:
        """Whether a session may be running somewhere other than here.

        A runtime that routes is believed, and only "cloud" is elsewhere.
        One that does not route, or a lookup that fails, has no opinion, and
        the answer is yes: the rule from before routing, which goes on to
        ask the runtime's capabilities. focus_task decides the same way.
        """
        locate = getattr(self.runtime, "location_of", None)
        if not callable(locate) or not session_id:
            return True
        try:
            answer = locate(session_id)
        except Exception:
            return True
        return answer == "cloud" if isinstance(answer, str) else True

    async def inspect_task(self, task_id: str) -> dict:
        conductor, task = self._find_task(task_id)
        self._touch(task.project_id, task.id)
        report = conductor.inspect_task(task_id)
        # The canonical answer to "why isn't it doing anything?" - health
        # and pending approvals come from the adapter, never from reading
        # terminal output (spec section 28).
        if task.provider_session_id:
            report["provider_health"] = await self.runtime.reconcile_session(
                task.provider_session_id)
            # A cloud worker reports nothing on its own, so "what is it
            # doing?" is the moment to go and look. Expensive - a process
            # and up to a minute - which is why it happens here, when
            # somebody asked, and never on a timer.
            #
            # Only a worker the router places in the cloud is looked at, the
            # rule focus_task already follows. Asking which capabilities the
            # runtime offers sent every session the routing map had never
            # heard of - every worker after a restart, since that map lives
            # in memory - to the first runtime with is_readable: the cloud
            # one, which answered "not readable" for a local worker, and
            # peek then spent its whole 60 s timeout on `claude --teleport`.
            # Measured 2026-09-10/11: 51 inspect_task calls on workers from
            # before a restart took a median 61.0 s, and every utterance
            # typed into the Boss meanwhile waited behind them (median 45 s).
            # A cloud worker from before a restart is not looked at either:
            # nothing records that it was one, and a minute of teleport is
            # too much to spend on a guess.
            peek = getattr(self.runtime, "peek", None)
            readable = getattr(self.runtime, "is_readable", None)
            if self._placed_elsewhere(task.provider_session_id) \
                    and peek is not None and readable is not None and \
                    not readable(task.provider_session_id):
                try:
                    # The router answers None for a session whose runtime
                    # has no peek - a local worker - and None cannot be
                    # awaited. It was: every inspect_task on a local task
                    # logged cloud.peek_failed.
                    seen = peek(task.provider_session_id)
                    if inspect.isawaitable(seen):
                        seen = await seen
                except Exception as exc:
                    seen = None
                    self._emit("cloud.peek_failed", "task", task_id=task.id,
                               severity="warning",
                               data={"error": str(exc)[:200]})
                if seen and seen.get("ok"):
                    report["latest"] = seen.get("said", "")
                    report["where"] = "cloud"
                    if seen.get("asked"):
                        report["waiting_on_you"] = seen["asked"]
            pending = await self.runtime.pending_approvals(
                task.provider_session_id)
            if pending:
                report["pending_approvals"] = pending
        # The subagent view: supervision status and the structured result -
        # the Manager reads outcomes here, never from transcripts.
        subagent = await self.subagent_for(task_id)
        report["subagent_status"] = subagent.status
        if subagent.result is not None:
            report["result"] = subagent.result.to_dict()
        return report

    async def send_to_task(self, task_id: str, message: str,
                           project_id: str | None = None) -> None:
        """Deliver a follow-up in the user's words, whatever the manager
        wrote.

        The manager prompt already says to relay verbatim - "you are a
        switchboard here, not an author" - and the manager still sends
        things like "Here's the actual instruction, read-only: run the gh
        commands yourself (don't just tell the user what to run) ...":
        framing the user never said, and an instruction they never gave.
        Measured at 23:59 tonight, after the prompt was tightened. A rule a
        model follows half the time is not a rule, so it lives here.
        """
        conductor, task = self._find_task(task_id)
        message, replaced = hold_to_users_words(message, self._utterance)
        if replaced:
            self._emit("manager.follow_up_rewritten", "manager",
                       task_id=task.id, severity="warning",
                       data={"manager_wrote": replaced[:300],
                             "sent_instead": message[:300]})
        await conductor.send_to_task(task_id, message)
        self._touch(task.project_id, task.id)

    async def interrupt_task(self, task_id: str,
                             project_id: str | None = None) -> None:
        conductor, task = self._find_task(task_id)
        await conductor.interrupt_task(task_id)
        self._touch(task.project_id, task.id)

    async def pause_task(self, task_id: str,
                         project_id: str | None = None) -> None:
        """Deliberate suspension: the session stays alive and associated,
        events are recorded but do not move the lifecycle, and only an
        explicit resume lifts it. paused != interrupted != dead."""
        conductor, task = self._find_task(task_id)
        if task.status == "paused":
            return                    # idempotent
        if task.provider_session_id:
            try:
                await self.runtime.interrupt(task.provider_session_id)
            except Exception as exc:
                self._emit("runtime.pause_interrupt_failed", "runtime",
                           task_id=task_id, severity="warning",
                           data={"error": str(exc)[:200]})
        conductor.store.update(task_id, status="paused")
        task_events.append(conductor.store, task_id, "paused")
        self._emit("task.paused", "task", task_id=task_id,
                   project_id=task.project_id)
        self._touch(task.project_id, task.id)

    async def resume_task(self, task_id: str,
                          project_id: str | None = None) -> None:
        conductor, task = self._find_task(task_id)
        if task.provider_session_id and \
                await self.runtime.get_status(
                    task.provider_session_id) == "running":
            if task.status == "paused":
                conductor.store.update(task_id, status="running")
            self._touch(task.project_id, task.id)
            return                    # idempotent: already running is a no-op
        if task.status == "paused" and task.provider_session_id and \
                await self.runtime.get_status(
                    task.provider_session_id) != "disconnected":
            # The same live session picks the work back up.
            await self.runtime.send(task.provider_session_id,
                                    "Please continue with the task.")
            conductor.store.update(task_id, status="running")
            task_events.append(conductor.store, task_id, "resumed")
            self._emit("task.resumed", "task", task_id=task_id,
                       provider_session_id=task.provider_session_id)
            self._touch(task.project_id, task.id)
            return
        await conductor.resume_task(task_id)
        self._touch(task.project_id, task.id)

    async def supervise_approvals(self) -> list[dict]:
        """The ApprovalSupervisor: takes responsibility for every pending
        approval. Routine ones are approved and forbidden ones denied -
        delivered into the same live session and verified (an approval is
        only handled once the worker demonstrably advanced) - and only the
        genuinely consequential remain escalated to the user by voice and
        context. Also the safety net: if a routine approval leaks past a
        runtime's own policy, it is resolved here instead of blocking."""
        actions: list[dict] = []
        for task_id, item in list(self._attention.items()):
            if item.get("kind") != "approval" or not item.get("approval_id"):
                continue
            decision = self.approval_policy.supervise(item["text"])
            if decision == "ask":
                continue              # the user's call; escalation stands
            self._emit("approval.policy_decision", "runtime",
                       task_id=task_id,
                       data={"approval_id": item["approval_id"],
                             "decision": decision, "by": "supervisor",
                             "description": item["text"][:150]})
            try:
                await self._resolve_approval(task_id, item["approval_id"],
                                             approve=decision == "allow")
                actions.append({"task_id": task_id,
                                "approval_id": item["approval_id"],
                                "decision": decision})
            except Exception as exc:
                # Delivery failure keeps the escalation: the worker is
                # still blocked and the user must hear about it.
                self._emit("approval.supervision_failed", "runtime",
                           task_id=task_id, severity="error",
                           data={"approval_id": item["approval_id"],
                                 "error": str(exc)[:200]})
        return actions

    async def approve_task_action(self, task_id: str, approval_id: str,
                                  project_id: str | None = None) -> None:
        await self._resolve_approval(task_id, approval_id, approve=True)

    async def deny_task_action(self, task_id: str, approval_id: str,
                               project_id: str | None = None) -> None:
        await self._resolve_approval(task_id, approval_id, approve=False)

    async def _resolve_approval(self, task_id: str, approval_id: str,
                                approve: bool) -> None:
        """Validated, id-addressed, idempotent - and VERIFIED: the decision
        must demonstrably reach the exact live worker. Updating our own
        state is not an approval; the provider clearing the gate is."""
        conductor, task = self._find_task(task_id)
        if not task.provider_session_id:
            raise RuntimeError(f"{task_id} has no provider session")
        decision = "approved" if approve else "denied"
        self._emit("approval.decision_sending", "runtime", task_id=task_id,
                   provider_session_id=task.provider_session_id,
                   data={"approval_id": approval_id, "decision": decision})
        await self.runtime.resolve_approval(task.provider_session_id,
                                            approval_id, approve)
        self._emit("approval.decision_sent", "runtime", task_id=task_id,
                   data={"approval_id": approval_id, "decision": decision})
        # Acknowledgement: the approval must have left the pending set. A
        # decision that was sent but did not unblock the worker is an
        # approval-delivery failure, not a generic stuck session.
        still_pending = [a for a in await self.runtime.pending_approvals(
                             task.provider_session_id)
                         if a.get("approval_id") == approval_id]
        if still_pending:
            self._emit("approval.delivery_failed", "runtime",
                       task_id=task_id, severity="error",
                       data={"approval_id": approval_id,
                             "decision": decision})
            raise RuntimeError(f"approval {approval_id} was sent but the "
                               "worker did not advance past it")
        conductor.store.update(task_id, status="running")
        task_events.append(conductor.store, task_id,
                           "approval_approved" if approve
                           else "approval_denied", approval_id=approval_id)
        self._emit("task.approval_resolved", "task", task_id=task_id,
                   project_id=task.project_id,
                   data={"approval_id": approval_id, "approved": approve})
        self._touch(task.project_id, task.id)

    # -- recovery ----------------------------------------------------------
    async def recover_task(self, task_id: str) -> dict:
        """Single-flight, understand-before-duplicating recovery. Concurrent
        triggers await the same operation. Preference order: reuse -> resume
        -> reconstruct; a replacement session or surface is the last resort,
        and only after the session is positively unrecoverable."""
        existing = self._recoveries.get(task_id)
        if existing is not None and not existing.done():
            return await asyncio.shield(existing)
        operation = asyncio.ensure_future(self._recover(task_id))
        self._recoveries[task_id] = operation
        try:
            return await operation
        finally:
            self._recoveries.pop(task_id, None)

    async def _recover(self, task_id: str) -> dict:
        conductor, task = self._find_task(task_id)
        sid = task.provider_session_id
        self._emit("recovery.started", task_id=task_id,
                   project_id=task.project_id)
        health = await self.runtime.reconcile_session(sid) if sid \
            else "missing"
        self._emit("recovery.health_checked", task_id=task_id,
                   data={"health": health})

        if health in ("starting", "running", "idle", "waiting_for_input",
                      "waiting_for_approval", "completed"):
            return {"health": health, "action": "none"}   # not dead: reuse

        if health == "unreachable":
            cwd = task.workspace.path if task.workspace else str(
                self.projects.get(task.project_id).root_path)
            for attempt in range(2):
                try:
                    await self.runtime.resume(sid, working_directory=cwd)
                    await conductor._subscribe(conductor.store.get(task_id))
                    conductor.store.update(task_id, status="running")
                    self._emit("recovery.session_resumed", task_id=task_id)
                    return {"health": "running", "action": "resumed"}
                except Exception as exc:
                    self._emit("recovery.resume_attempt_failed",
                               task_id=task_id, severity="warning",
                               data={"attempt": attempt + 1,
                                     "error": str(exc)[:200]})
                    await asyncio.sleep(0.05 * (attempt + 1))
            health = "missing"        # resume failed twice: confirmed gone

        # Confirmed missing: reconstruct from durable state (spec 32). Task
        # identity, workspace and context all survive worker replacement.
        from . import task_context
        cwd = task.workspace.path if task.workspace else str(
            self.projects.get(task.project_id).root_path)
        context = task_context.read(conductor.store, task_id)
        prompt = (f"You are replacing a previous worker on this task.\n\n"
                  f"{build_worker_prompt(task, self.project_context_for(task))}"
                  f"\n\nWhat is known so far:\n{context[:2000]}")
        try:
            new_sid = await self.runtime.create_session(
                task_id=task_id, working_directory=cwd,
                initial_prompt=prompt)
        except Exception as exc:
            conductor.store.update(task_id, status="interrupted")
            self._emit("recovery.failed", task_id=task_id, severity="error",
                       data={"error": str(exc)[:200]})
            return {"health": "missing", "action": "failed"}
        conductor._unsubscribe(task_id)
        task = conductor.store.update(task_id, provider_session_id=new_sid,
                                      status="running")
        await conductor._subscribe(task)
        task_events.append(conductor.store, task_id, "session_reconstructed",
                           provider_session_id=new_sid)
        self._emit("recovery.session_reconstructed", task_id=task_id,
                   provider_session_id=new_sid)
        # Prefer the existing surface; only a confirmed-missing one is
        # replaced, and only for visible tasks (spec section 33).
        if self.surfaces and task.surface_mode == "visible":
            handle = SurfaceHandle.from_dict(task.surface) if task.surface \
                else None
            impl = self._surface_impl(handle)
            if handle and impl is not None and await asyncio.to_thread(impl.is_available, handle):
                self._emit("recovery.surface_reused", task_id=task_id)
            else:
                conductor.store.update(task_id, surface=None)
                await self._open_surface(conductor.store.get(task_id),
                                         reason="provider_reconstruction")
                self._emit("recovery.surface_recreated", task_id=task_id)
        return {"health": "running", "action": "reconstructed",
                "provider_session_id": new_sid}

    def project_context_for(self, task: Task) -> str:
        try:
            return self.projects.context_path(task.project_id).read_text()
        except OSError:
            return ""

    async def complete_task(self, task_id: str,
                            project_id: str | None = None) -> None:
        """The user says this one is done. See Conductor.complete_task for
        why nothing else can: a worker that has answered is waiting, not
        finished, until somebody says the work is over."""
        conductor, task = self._find_task(task_id)
        await conductor.complete_task(task_id)
        self._attention.pop(task_id, None)
        self._touch(task.project_id, task.id)

    async def cancel_task(self, task_id: str,
                          project_id: str | None = None) -> None:
        conductor, task = self._find_task(task_id)
        await conductor.cancel_task(task_id)
        self._touch(task.project_id, task.id)

    async def handoff_task_context(self, from_task_id: str, to_task_id: str,
                                   note: str | None = None) -> None:
        """Give one task another task's semantic findings (spec v2 section
        38): the source's context.md - never its raw transcript - reaches the
        destination worker and its durable context. Works across projects."""
        from . import task_context
        source_conductor, source = self._find_task(from_task_id)
        if from_task_id == to_task_id:
            raise ValueError("a task cannot hand off to itself")
        context = task_context.read(source_conductor.store,
                                    from_task_id).strip()
        if not context:
            raise RuntimeError(f"{from_task_id} has no context to hand off")
        message = (f"Context from the task \"{source.title}\" "
                   f"({from_task_id}):\n\n{context[:2000]}")
        if note:
            message += f"\n\nNote from the user: {note}"
        dest_conductor, dest = self._find_task(to_task_id)
        await dest_conductor.send_to_task(to_task_id, message)
        self._emit("task.handoff", "task", task_id=to_task_id,
                   project_id=dest.project_id,
                   data={"from_task_id": from_task_id})
        self._touch(dest.project_id, dest.id)

    # -- the manager seam --------------------------------------------------------
    async def handle_action(self, tool: str, args: dict | None = None):
        args = args or {}
        self._emit("conductor.action_received",
                   data={"tool": tool, "args": args})
        if tool not in MANAGER_TOOLS:
            self._emit("conductor.error", severity="error",
                       data={"tool": tool, "error": "unknown tool"})
            raise ValueError(f"unknown manager tool: {tool!r}")
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
                   project_id=args.get("project_id"),
                   task_id=args.get("task_id"),
                   duration_ms=round((time.monotonic() - started) * 1000, 1),
                   data={"tool": tool})
        return result

    def global_context(self, max_projects: int = 12) -> str:
        """The compact opening context for a Manager turn: recent projects
        with activity counts and the conversational focus - never every task
        of every project (spec 22), and never linear in the registry: beyond
        max_projects, only projects with active work are named, the rest are
        a count, each reachable by name through create_task."""
        lines = []
        # Outstanding decisions come first: a worker waiting on the user is
        # the most important thing in any turn, and the Manager acts on it
        # (resolve it if the utterance answers it; otherwise mention it).
        if self._attention:
            lines.append("NEEDS YOUR DECISION:")
            for task_id, item in list(self._attention.items())[:4]:
                suffix = (f" (approval_id {item['approval_id']})"
                          if item.get("approval_id") else "")
                lines.append(f"  {task_id} needs "
                             f"{'approval' if item['kind'] == 'approval' else 'an answer'}"
                             f": {item['text']}{suffix}")
            lines.append("")
        inbox = getattr(self, "inbox", None)
        if inbox is not None:
            digest = inbox.digest_for_manager()
            if digest:
                lines.append(digest)
                lines.append("")
        recent = self.projects.recent_ids()
        projects = {p.id: p for p in self.projects.list()}
        ordered = [projects[pid] for pid in recent if pid in projects]
        ordered += [p for p in projects.values() if p.id not in recent]
        if not ordered:
            lines.append("No projects are registered yet.")
        else:
            shown, hidden = [], 0
            for project in ordered:
                tasks = self._conductor(project.id).list_tasks()
                active = [t for t in tasks if t.status in ACTIVE_STATUSES]
                # Most recently addressed first: when titles repeat, order
                # and recency are the only things telling them apart.
                active.sort(key=lambda t: t.last_instruction_at or t.created_at,
                            reverse=True)
                if len(shown) < max_projects or active:
                    shown.append((project, active))
                else:
                    hidden += 1
            counts = self.session_counts()
            lines.append(
                f"Open sessions: {counts['open']}"
                + (f" ({counts['hidden_running']} more running hidden)"
                   if counts["hidden_running"] else "")
                + f". {counts['resumable']} older sessions are resumable "
                  "through search_sessions - they are not open.")
            lines.append("Known projects (most recent first):")
            for project, active in shown:
                summary = (f"- {project.display_name} ({project.id}): "
                           f"{len(active)} active task(s)")
                for task in active[:4]:
                    summary += (f"\n    {task.id} [{task.status}] "
                                f"{task.title} - last message "
                                f"{idle_for(task)}")
                lines.append(summary)
            if hidden:
                lines.append(f"...and {hidden} more registered projects; "
                             "name one in create_task to reach it.")
        focus = self.projects.focus()
        if focus.get("project_id"):
            lines.append(f"Recent focus: project {focus['project_id']}"
                         + (f", task {focus['task_id']}"
                            if focus.get("task_id") else ""))
        return "\n".join(lines)

    async def handle_user_message(self, text: str,
                                  source: str = "text",
                                  trace_id: str = "",
                                  utterance: str = "") -> ManagerTurn:
        """utterance is what the microphone heard, verbatim, when this turn
        was spoken. It is kept for the length of the turn so a follow-up
        the manager sends can be held to the user's words."""
        if self.manager is None:
            raise RuntimeError("no ManagerBackend configured")
        # Not serialized here. The backend decides: the SDK Boss takes
        # one turn at a time on its one response stream; the visible Boss
        # types the words in at once and Claude Code queues them. A lock
        # here used to hold the second thing said until the first was
        # answered - the words never reached the window while the Boss
        # was thinking. The utterance bookkeeping that lock protected is
        # a list now (_utterances).
        if self._open_turns:
            self._emit("manager.turn_overlapping", "manager",
                       data={"source": source, "text": text[:300],
                             "open_turns": self._open_turns})
        return await self._turn(text, source, trace_id, utterance)

    def _refresh_utterance(self) -> None:
        self._utterance = " ".join(self._utterances)

    async def _turn(self, text: str, source: str, trace_id: str,
                    utterance: str) -> ManagerTurn:
        words = (utterance or "").strip()
        if not isinstance(self._utterances, list):
            self._utterances = []
        if words:
            self._utterances.append(words)
        self._refresh_utterance()
        if not self._open_turns:
            self._voice_notes: list[str] = []  # note_for_voice, this turn
        self._open_turns += 1
        # Everything pending when this turn starts is what the Manager
        # will have read; it is acknowledged when the turn ends, however
        # the turn ends. A turn that fails leaves it pending for the next.
        inbox = getattr(self, "inbox", None)
        seen_in_turn = ([e.event_id for e in inbox.pending_for_manager()]
                        if inbox is not None else [])
        # A spoken turn already has a trace, opened when the key went down;
        # adopt it so the hold and the transcript sit on the same trace as
        # the routing decision they caused.
        new_trace(trace_id)
        started = time.monotonic()
        self._emit("manager.turn_started", "manager",
                   data={"source": source, "text": text[:300],
                         "tasks": [task_summary(t)
                                   for t in self.list_tasks()],
                         "projects": self.list_projects()})
        try:
            turn = await self.manager.handle(text, self)
        except Exception as exc:
            self._emit("manager.error", "manager", severity="error",
                       data={"error": str(exc)[:300]})
            raise
        finally:
            self._open_turns -= 1
            if words in self._utterances:
                self._utterances.remove(words)
            self._refresh_utterance()
        notes = [n for n in getattr(self, "_voice_notes", []) if n]
        self._voice_notes = []
        if notes:
            turn.commentary = "\n".join(notes)
        # The digest rides in global_context, which only a headless
        # manager reads. A manager that types its updates
        # (deliver_supervisory) acks each one when the push that carries
        # it lands; a turn here must not mark delivered what was never
        # typed into its window.
        if inbox is not None and seen_in_turn and \
                not hasattr(self.manager, "deliver_supervisory"):
            inbox.ack_manager(seen_in_turn)
            self._emit("supervisor.delivered", "manager",
                       data={"event_ids": seen_in_turn[:20],
                             "count": len(seen_in_turn)})
        for call in turn.tool_calls:
            self._emit("manager.tool_call", "manager",
                       project_id=call.args.get("project_id"),
                       task_id=call.args.get("task_id"),
                       data={"tool": call.tool, "args": call.args})
        self._emit("manager.turn_completed", "manager",
                   duration_ms=round((time.monotonic() - started) * 1000, 1),
                   data={"reply": turn.reply[:300],
                         "tools": [call.tool for call in turn.tool_calls]})
        return turn

    def _relocate(self, project: Project) -> Project:
        """A missing path with exactly one fingerprint match elsewhere is the
        same project, moved: reconnect its identity. Two plausible copies are
        never chosen between silently - the project is marked moved and the
        Manager asks when it is next needed (spec section 60)."""
        matches = self.locator.find_moved(project.id)
        if len(matches) == 1:
            project = self.projects.update(project.id,
                                           root_path=matches[0].path,
                                           repo_root=matches[0].repo_root,
                                           status="available")
            self._emit("conductor.project_relocated", project_id=project.id,
                       data={"path": project.root_path})
        elif matches:
            project = self.projects.update(project.id, status="moved")
            self._emit("conductor.project_move_ambiguous",
                       project_id=project.id, severity="warning",
                       data={"candidates": [m.path for m in matches[:5]]})
        return project

    # -- startup recovery ---------------------------------------------------------
    async def startup(self, resume: bool = False) -> dict:
        """Verify project paths, then take stock of each project's tasks.
        Missing projects are marked, never deleted (spec 59).

        Nothing is woken by default: launching must not spawn workers. Pass
        resume=True for an eager reconnect (recovery drills, tests).
        """
        self._emit("storage.recovery_started", "storage")
        recovered, missing = [], []
        for project in self.projects.list():
            project = self.locator.refresh(project.id)
            if project.status != "available":
                project = self._relocate(project)
            if project.status != "available":
                missing.append(project.id)
                continue
            recovered.extend(await self._conductor(project.id).startup(
                resume=resume))
        # Reconcile surfaces rather than reopening them: stale handles are
        # cleared, but no historical windows are spawned (spec section 25) -
        # a surface reappears on demand via focus_task or a notification.
        if self.surfaces:
            for task in self.list_tasks():
                if not task.surface:
                    continue
                handle = SurfaceHandle.from_dict(task.surface)
                impl = self._surface_impl(handle)
                if impl is None or not await asyncio.to_thread(impl.is_available, handle):
                    self._conductor(task.project_id).store.update(
                        task.id, surface=None)
                    self._emit("surface.closed", task_id=task.id,
                               project_id=task.project_id)
        # Live workers are read from now, not from the first sweep.
        watched = await self.watch_unwatched()
        self._emit("storage.recovery_completed", "storage",
                   data={"recovered": len(recovered),
                         "missing_projects": missing,
                         "watched": len(watched)})
        return {"recovered_tasks": recovered, "missing_projects": missing,
                "watched": watched}
