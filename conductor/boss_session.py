"""BossSession: the session the user is talking to, as a durable thing.

    VoiceConversation  <->  BossSession  ->  Managed Subagents (children)

The Boss used to be an invisible model call: a real Claude Code session
with a transcript on disk, but nothing of ours recorded what the user
told it, what it decided, which tools it called, what came back, which
workers it started or messaged, or what it heard from them. Whatever
leaked into a notification card was the only record, and it left with
the card.

This module is the record. One BossSession per voice conversation, one
ordered timeline of BossSessionEvents per BossSession, both on disk,
both reconstructable without any UI having survived:

    1  user_message        "Fix login in Posely"          (voice)
    2  status              Searching projects...
    3  tool_started        find_project  {"query": "Posely"}
    3  tool_completed      -> ~/Developer/posely           (same item)
    4  subagent_created    Posely · Fix login   sub_task_a
    5  boss_message        "I've started a worker on it."
    6  subagent_event_received  Posely · Fix login completed

Identity is the BossSession id and the sequence number, never a title
and never a position in a window. The Boss's own provider session id is
captured the moment it exists, so a restart resumes the same conversation
in the same window rather than starting another.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .storage import append_jsonl, atomic_write_json, now_iso, read_json, read_jsonl

BOSS_STATUSES = ("starting", "ready", "thinking", "waiting_for_tool",
                 "waiting_for_user", "interrupted", "recovering", "closed",
                 "failed")

EVENT_TYPES = ("user_message", "boss_message", "voice_message", "boss_interim",
               "status", "tool_started",
               "tool_completed", "tool_failed", "subagent_created",
               "subagent_messaged", "subagent_event_received",
               "approval_action", "system_event")

# Tools whose execution is a child-session action, and how the timeline
# names them. Anything else renders as a plain tool.
CHILD_ACTIONS = {
    "create_task": ("subagent_created", "Start worker"),
    "send_to_task": ("subagent_messaged", "Message worker"),
    "approve_task_action": ("approval_action", "Approve worker action"),
    "deny_task_action": ("approval_action", "Deny worker action"),
}

READABLE_TOOL = {
    "find_project": "Find project", "list_projects": "List projects",
    "inspect_project": "Inspect project", "register_project": "Register project",
    "list_tasks": "List tasks", "list_subagents": "List workers",
    "list_open_sessions": "List open sessions",
    "list_recent_sessions": "List recent sessions",
    "search_sessions": "Search sessions", "situation": "Situation",
    "inspect_task": "Inspect worker", "pause_task": "Pause worker",
    "interrupt_task": "Interrupt worker", "resume_task": "Resume worker",
    "complete_task": "Close task", "cancel_task": "Cancel task",
    "focus_task": "Open worker", "handoff_task_context": "Hand off context",
}


def new_boss_id() -> str:
    return "boss_" + secrets.token_hex(4)


def new_conversation_id() -> str:
    return "conv_" + secrets.token_hex(4)


@dataclass
class BossSession:
    id: str
    conversation_id: str
    provider_session_id: str | None = None
    status: str = "starting"
    title: str = "New voice chat"
    child_subagent_ids: list[str] = field(default_factory=list)
    surface: dict | None = None            # SurfaceHandle.to_dict(), if any
    workspace: str = ""                    # the Boss's own directory
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.status not in BOSS_STATUSES:
            raise ValueError(f"unknown boss status: {self.status!r}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BossSession":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known["child_subagent_ids"] = list(known.get("child_subagent_ids") or [])
        return cls(**known)


@dataclass
class BossSessionEvent:
    id: str
    boss_session_id: str
    sequence: int
    type: str
    timestamp: str = field(default_factory=now_iso)
    trace_id: str = ""
    payload: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown boss event type: {self.type!r}")

    def to_dict(self) -> dict:
        return asdict(self)


class BossSessionStore:
    """On disk under <home>/boss/sessions/<id>/: session.json and
    timeline.jsonl. Plus <home>/boss/conversations.json binding voice
    conversations to Boss sessions, and which conversation is current."""

    def __init__(self, home: str | Path) -> None:
        self.root = Path(home) / "boss"
        self._lock = threading.Lock()
        self._sequence: dict[str, int] = {}

    # -- paths ------------------------------------------------------------
    def session_dir(self, boss_id: str) -> Path:
        return self.root / "sessions" / boss_id

    def _session_path(self, boss_id: str) -> Path:
        return self.session_dir(boss_id) / "session.json"

    def _timeline_path(self, boss_id: str) -> Path:
        return self.session_dir(boss_id) / "timeline.jsonl"

    def _bindings_path(self) -> Path:
        return self.root / "conversations.json"

    # -- sessions ---------------------------------------------------------
    def save(self, session: BossSession) -> None:
        with self._lock:
            session.updated_at = now_iso()
            self.session_dir(session.id).mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._session_path(session.id), session.to_dict())

    def get(self, boss_id: str) -> BossSession | None:
        data = read_json(self._session_path(boss_id), default=None)
        if not data:
            return None
        try:
            return BossSession.from_dict(data)
        except (TypeError, ValueError):
            return None

    def list(self) -> list[BossSession]:
        out = []
        sessions = self.root / "sessions"
        if not sessions.is_dir():
            return out
        for path in sorted(sessions.iterdir()):
            session = self.get(path.name)
            if session is not None:
                out.append(session)
        return out

    # -- conversations ------------------------------------------------------
    def _bindings(self) -> dict:
        data = read_json(self._bindings_path(), default=None) or {}
        data.setdefault("current", None)
        data.setdefault("conversations", {})
        return data

    def current_conversation(self) -> str:
        """The conversation the voice is in. Created if there is none:
        the product starts in a conversation, not in limbo."""
        with self._lock:
            data = self._bindings()
            if not data["current"]:
                data["current"] = new_conversation_id()
                data["conversations"][data["current"]] = {
                    "boss_session_id": None, "created_at": now_iso()}
                self.root.mkdir(parents=True, exist_ok=True)
                atomic_write_json(self._bindings_path(), data)
            return data["current"]

    def new_conversation(self) -> str:
        """"New voice chat": a distinct conversation, and therefore a new
        Boss on its first turn. Closing a window is not this."""
        with self._lock:
            data = self._bindings()
            data["current"] = new_conversation_id()
            data["conversations"][data["current"]] = {
                "boss_session_id": None, "created_at": now_iso()}
            self.root.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._bindings_path(), data)
            return data["current"]

    def resume_conversation(self, conversation_id: str) -> str:
        """Make an old conversation current again: its Boss - already
        bound - is the one the next turn resumes. Unknown ids refuse."""
        with self._lock:
            data = self._bindings()
            if conversation_id not in data["conversations"]:
                raise KeyError(conversation_id)
            data["current"] = conversation_id
            self.root.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._bindings_path(), data)
            return conversation_id

    def boss_for(self, conversation_id: str) -> BossSession | None:
        data = self._bindings()
        boss_id = (data["conversations"].get(conversation_id) or {}).get(
            "boss_session_id")
        return self.get(boss_id) if boss_id else None

    def bind(self, conversation_id: str, boss_id: str) -> None:
        with self._lock:
            data = self._bindings()
            data["conversations"].setdefault(conversation_id, {
                "created_at": now_iso()})["boss_session_id"] = boss_id
            self.root.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._bindings_path(), data)

    def current_boss(self) -> BossSession | None:
        """currentBossSessionId, canonically - from the binding, never
        from a window or the last model request."""
        return self.boss_for(self.current_conversation())

    # -- timeline -------------------------------------------------------------
    def events(self, boss_id: str) -> list[BossSessionEvent]:
        out = []
        for entry in read_jsonl(self._timeline_path(boss_id)):
            try:
                out.append(BossSessionEvent(**entry))
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda e: e.sequence)
        return out

    def append(self, boss_id: str, type_: str, payload: dict | None = None,
               trace_id: str = "", event_id: str | None = None) -> BossSessionEvent:
        with self._lock:
            if boss_id not in self._sequence:
                existing = read_jsonl(self._timeline_path(boss_id))
                self._sequence[boss_id] = max(
                    (int(e.get("sequence", 0)) for e in existing), default=0)
            self._sequence[boss_id] += 1
            event = BossSessionEvent(
                id=event_id or "bev_" + secrets.token_hex(5),
                boss_session_id=boss_id, sequence=self._sequence[boss_id],
                type=type_, trace_id=trace_id, payload=dict(payload or {}))
            self.session_dir(boss_id).mkdir(parents=True, exist_ok=True)
            append_jsonl(self._timeline_path(boss_id), event.to_dict())
            return event


# -- rendering -------------------------------------------------------------------

# What the Boss's tools are called on the timeline, as a group: the
# product's name for them, never the transport's (no server name, no
# socket path, no localhost URL - those belong in diagnostics).
NAMESPACE = "Agent Control"


def tool_label(tool: str) -> str:
    if tool in CHILD_ACTIONS:
        label = CHILD_ACTIONS[tool][1]
    else:
        label = READABLE_TOOL.get(tool, tool.replace("_", " ").capitalize())
    return f"{NAMESPACE} · {label}"


def input_summary(tool: str, args: dict) -> str:
    """One readable line for what a tool was asked, never raw JSON."""
    args = args or {}
    if tool == "create_task":
        return str(args.get("title") or args.get("goal") or "")[:120]
    if tool == "send_to_task":
        return f'{args.get("task_id", "")}: "{str(args.get("message", ""))[:100]}"'
    if tool in ("find_project", "search_sessions"):
        return f'"{args.get("query", "")}"'
    if "task_id" in args:
        return str(args["task_id"])
    if "project_id" in args:
        return str(args["project_id"])
    if "path" in args:
        return str(args["path"])
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items())[:120]


def output_summary(tool: str, result: str) -> str:
    """The result, shortened for reading. The full text stays in the
    event payload for expansion."""
    text = " ".join((result or "").split())
    if not text:
        return "done"
    try:
        data = json.loads(text)
    except ValueError:
        # Tool results are cut at a few thousand characters, so a long
        # list arrives as JSON that no longer parses. Measured: the Boss's
        # list_open_sessions rendered as `[{"subagent_id": ...` on the
        # timeline. Count what is countable instead of printing the cut.
        if text.startswith("[") and '"task_id"' in text:
            return f"{text.count(chr(34) + 'task_id' + chr(34))} item(s), listing cut short"
        return text[:160]
    if isinstance(data, dict):
        if "task_id" in data and "title" in data:
            return f"{data['title']} ({data['task_id']})"
        if "path" in data or "root_path" in data:
            return str(data.get("path") or data.get("root_path"))
        return text[:160]
    if isinstance(data, list):
        return f"{len(data)} item(s)" if data else "nothing"
    return text[:160]


def render_timeline(events: list[BossSessionEvent], title: str = "") -> str:
    """The Boss session as a person reads it. Tool start and finish are
    one item: the finish rewrites the start's line."""
    lines: list[str] = []
    if title:
        lines += [title, "=" * len(title), ""]
    tools: dict[str, int] = {}          # execution id -> line index
    for event in events:
        p = event.payload
        if event.type == "user_message":
            source = p.get("source", "voice")
            lines += [f"YOU{' (typed)' if source == 'typed' else ''}",
                      f"  {p.get('text', '')}", ""]
        elif event.type == "boss_message":
            lines += ["BOSS", f"  {p.get('text', '')}", ""]
        elif event.type == "voice_message":
            lines += ["VOICE", f"  {p.get('text', '')}", ""]
        elif event.type == "status":
            lines += [f"  … {p.get('text', '')}", ""]
        elif event.type == "tool_started":
            tools[p.get("execution_id", "")] = len(lines)
            asked = input_summary(p.get("tool", ""), p.get("args") or {})
            lines += [f"▶ {tool_label(p.get('tool', ''))}"]
            if asked:                     # a no-argument tool has no line
                lines.append(f"  {asked}")
            lines.append("")
        elif event.type in ("tool_completed", "tool_failed"):
            index = tools.get(p.get("execution_id", ""))
            mark = "✓" if event.type == "tool_completed" else "!"
            summary = p.get("output_summary") or p.get("error", "")
            if index is not None:
                lines[index] = f"{mark} {tool_label(p.get('tool', ''))}"
                # Right after the heading and its argument line, if any.
                at = index + 1
                while at < len(lines) and lines[at].startswith("  "):
                    at += 1
                lines.insert(at, f"  {summary}")
            else:
                lines += [f"{mark} {tool_label(p.get('tool', ''))}",
                          f"  {summary}", ""]
        elif event.type == "subagent_created":
            lines += ["◉ Worker started",
                      f"  {p.get('title', '')}    [Open {p.get('subagent_id', '')}]",
                      ""]
        elif event.type == "subagent_messaged":
            lines += ["◉ Worker messaged",
                      f"  {p.get('title') or p.get('task_id', '')}",
                      f'  "{p.get("message", "")}"', ""]
        elif event.type == "subagent_event_received":
            lines += ["◉ Worker update",
                      f"  {p.get('title') or p.get('task_id', '')}",
                      f"  {p.get('summary', '')}", ""]
        elif event.type == "approval_action":
            lines += [f"◉ {p.get('decision', 'Decision').capitalize()}",
                      f"  {p.get('title') or p.get('task_id', '')}", ""]
        elif event.type == "system_event":
            lines += [f"  [{p.get('text', '')}]", ""]
    return "\n".join(lines).rstrip() + "\n"
