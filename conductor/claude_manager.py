"""ClaudeManagerBackend: the Manager as a persistent Claude session.

The Manager gets exactly the Conductor's tool surface as in-process MCP
tools - no Bash, no filesystem, no session discovery. Against a
GlobalConductor that is eleven tools (four project + seven task) with
hierarchical context: recent projects and focus up front, deeper detail via
inspect_project / inspect_task. Against the older per-project Conductor it
degrades to the seven task tools and a full registry block.

Imported separately from manager.py so nothing else pays for the SDK import.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                              ClaudeSDKClient, TextBlock,
                              create_sdk_mcp_server, tool)

from .manager import (ManagerBackend, ManagerTurn, ToolCall,
                      clock_block, registry_block)
from .capabilities import capability_block
from .observability import ObservabilityEvent, application_log
from .task_types import Task

MANAGER_PROMPT_VERSION = "manager-v22"
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts/manager.md"

_FALLBACK_PROMPT = (
    "You conduct coding agents across the user's local projects. Resolve "
    "which project and task each message means using your tools, then act. "
    "Ask one short question when a reference is materially ambiguous. Reply "
    "in one or two spoken sentences.")

# The tool definitions live in boss_tools, dependency-free, so the same
# names, schemas and descriptions reach the SDK backend here, boss-mcp
# serving a real Claude Code session, and the startup gate that checks
# the Boss has what it needs.
from .boss_tools import (DESCRIPTIONS as _DESCRIPTIONS,
                         OPTIONAL as _OPTIONAL, SCHEMAS as _SCHEMAS)

_JSON_TYPES = {str: "string", bool: "boolean", int: "integer",
               float: "number"}


def _sdk_schema(name: str) -> dict:
    """The schema the SDK's tool() gets. A plain {name: type} dict makes
    every parameter required; a tool with optional parameters is spelled
    out as JSON Schema so the required list can leave them off."""
    optional = _OPTIONAL.get(name)
    if not optional:
        return _SCHEMAS[name]
    return {"type": "object",
            "properties": {key: {"type": _JSON_TYPES[kind]}
                           for key, kind in _SCHEMAS[name].items()},
            "required": [key for key in _SCHEMAS[name]
                         if key not in optional]}


def load_manager_prompt() -> str:
    try:
        text = _PROMPT_PATH.read_text()
    except OSError:
        application_log("manager", "manager.prompt_load_failed",
                        "falling back to the built-in Manager prompt",
                        severity="warning", exc_info=True,
                        path=str(_PROMPT_PATH))
        return _FALLBACK_PROMPT
    _, _, body = text.partition("\n---\n")
    return (body or text).strip() or _FALLBACK_PROMPT


def _serialize(result) -> str:
    if result is None:
        return "ok"
    if isinstance(result, Task):
        result = {"task_id": result.id, "project_id": result.project_id,
                  "title": result.title, "status": result.status}
    if isinstance(result, list):
        # Stack position travels with any list of tasks, so "the second one"
        # resolves from a tool result the same way it does from the prompt's
        # registry. Without it list_tasks answers with rows the Manager
        # cannot count, and it falls back to timestamps that all read
        # "just now".
        from .manager import stack_positions
        places = stack_positions([t for t in result if isinstance(t, Task)])
        rows = []
        for t in result:
            if not isinstance(t, Task):
                rows.append(t)
                continue
            row = {"task_id": t.id, "project_id": t.project_id,
                   "title": t.title, "status": t.status, "goal": t.goal}
            if t.id in places:
                nth, total = places[t.id]
                row["on_screen"] = f"{nth} of {total}"
            rows.append(row)
        result = sorted(
            rows, key=lambda r: int(r["on_screen"].split()[0])
            if isinstance(r, dict) and r.get("on_screen") else 999)
    if isinstance(result, dict) and "context" in result:
        result = dict(result)
        result["context"] = result["context"][:1500]
    return _fit(result)


# How much of a tool result the Boss is handed. MCP and the SDK both
# carry this without complaint; the old 4000 was sized for a spoken
# summary, and it was applied as a slice through the JSON text.
RESULT_LIMIT = 12_000


def _fit(result, limit: int = RESULT_LIMIT) -> str:
    """JSON the Boss can always parse, however big the data was.

    Measured over the live bridge: list_tasks came back exactly 4000
    characters long, cut mid-escape, and json.loads refused it. The
    invisible Boss read results as prose and could shrug at a ragged
    end; the visible Boss reads them over MCP as data, and one task
    with a long goal made every list_tasks unparseable for it.

    So the result is shortened as data: string fields lose their tails
    in rounds until it fits, and a list too long even then keeps the
    rows that fit and says how many it left out.
    """
    text = json.dumps(result, default=str)
    if len(text) <= limit:
        return text
    for max_str in (600, 300, 120):
        text = json.dumps(_shorten(result, max_str), default=str)
        if len(text) <= limit:
            return text
    if isinstance(result, list):
        kept = []
        for row in result:
            candidate = json.dumps(
                {"items": kept + [_shorten(row, 120)],
                 "omitted": len(result) - len(kept) - 1}, default=str)
            if len(candidate) > limit:
                break
            kept.append(_shorten(row, 120))
        return json.dumps({"items": kept, "omitted": len(result) - len(kept)},
                          default=str)
    # A single value that will not fit even shortened: say so, in JSON.
    return json.dumps({"truncated": True, "preview": text[:limit - 60]})


def _shorten(value, max_str: int):
    """The same structure, with every string cut to max_str (marked)."""
    if isinstance(value, str):
        return value if len(value) <= max_str else value[:max_str - 1] + "…"
    if isinstance(value, dict):
        return {k: _shorten(v, max_str) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shorten(v, max_str) for v in value]
    return value


def _manager_tools(conductor) -> tuple[str, ...]:
    if hasattr(conductor, "MANAGER_TOOLS"):
        return conductor.MANAGER_TOOLS
    from .global_conductor import GlobalConductor
    from .global_conductor import MANAGER_TOOLS as GLOBAL_TOOLS
    if isinstance(conductor, GlobalConductor):
        return GLOBAL_TOOLS
    from .conductor import MANAGER_TOOLS as TASK_TOOLS
    return TASK_TOOLS


class ClaudeManagerBackend(ManagerBackend):
    def __init__(self, model: str | None = None, max_turns: int = 32) -> None:
        self.model = model
        self.max_turns = max_turns
        self.prompt = load_manager_prompt()
        self._client: ClaudeSDKClient | None = None
        self._conductor = None
        self._turn_calls: list[ToolCall] = []
        # Made on first use so it binds to the loop the turns run on.
        self._turn_lock: asyncio.Lock | None = None

    @property
    def busy(self) -> bool:
        return self._turn_lock is not None and self._turn_lock.locked()

    # -- tools -------------------------------------------------------------
    def _make_tools(self, names: tuple[str, ...]) -> list:
        tools = []
        for name in names:
            def make(tool_name: str):
                async def handler(args: dict) -> dict:
                    try:
                        result = await self._conductor.handle_action(
                            tool_name, args)
                    except Exception as exc:
                        application_log(
                            "manager", "manager.tool_failed",
                            f"{tool_name} failed", severity="error",
                            exc_info=True, tool=tool_name,
                            task_id=args.get("task_id"),
                            project_id=args.get("project_id"))
                        self._turn_calls.append(ToolCall(
                            tool=tool_name, args=args,
                            result=f"error: {exc}"))
                        return {"content": [{"type": "text",
                                             "text": f"Error: {exc}"}],
                                "is_error": True}
                    text = _serialize(result)
                    self._turn_calls.append(ToolCall(tool=tool_name,
                                                     args=args, result=text))
                    return {"content": [{"type": "text", "text": text}]}
                return handler
            tools.append(tool(name, _DESCRIPTIONS[name],
                              _sdk_schema(name))(make(name)))
        return tools

    def _store(self):
        """Where the manager session id persists: the global store for a
        GlobalConductor, the task store otherwise."""
        return getattr(self._conductor, "projects", None) or \
            self._conductor.store

    async def _connect(self, conductor) -> ClaudeSDKClient:
        if self._client is not None:
            return self._client
        self._conductor = conductor
        names = _manager_tools(conductor)
        server = create_sdk_mcp_server("conductor",
                                       tools=self._make_tools(names))
        options = ClaudeAgentOptions(
            model=self.model,
            max_turns=self.max_turns,
            system_prompt=self.prompt,
            mcp_servers={"conductor": server},
            allowed_tools=[f"mcp__conductor__{name}" for name in names],
            disallowed_tools=["Bash", "Read", "Edit", "Write", "Glob",
                              "Grep", "NotebookEdit", "Task",
                              "Agent", "WebFetch", "WebSearch",
                              "AskUserQuestion"],
            permission_mode="bypassPermissions",
            resume=self._store().manager().get("session_id"),
        )
        self._client = ClaudeSDKClient(options)
        await self._client.connect()
        return self._client

    # -- ManagerBackend ------------------------------------------------------
    async def handle(self, text: str, conductor) -> ManagerTurn:
        """One turn at a time, in the order the messages arrived.

        The client has ONE response stream, and receive_response() hands
        each message to whichever caller is waiting on it. Two turns in
        flight at once therefore split one conversation between them:
        measured on a live run, a second utterance spoken while the
        Manager was mid-tool-loop was folded by the CLI into the running
        turn (queue enqueue -> remove, one result for both), the newer
        caller took that single result with an empty reply, and the older
        one waited for a result that never came - its delegation stayed
        open on the voice side for the rest of the session, so nothing was
        ever said about either request. When the first turn happened to be
        finishing instead, the callers swapped replies. Serializing here
        keeps each reply with the message that asked for it and means the
        CLI never sees a mid-turn message at all.
        """
        if self._turn_lock is None:
            self._turn_lock = asyncio.Lock()
        if self._turn_lock.locked():
            conductor.bus.emit(ObservabilityEvent(
                type="manager.turn_queued", component="manager",
                data={"text": text[:300]}))
        async with self._turn_lock:
            return await self._turn(text, conductor)

    async def _turn(self, text: str, conductor) -> ManagerTurn:
        client = await self._connect(conductor)
        self._conductor = conductor
        self._turn_calls = []

        # Fresh state every turn: local state is authoritative, manager
        # conversational memory is convenience.
        if hasattr(conductor, "global_context"):
            context = conductor.global_context()
        else:
            context = "Current tasks:\n" + \
                registry_block(conductor.list_tasks())
        # What this build can actually do, probed rather than assumed: a
        # supervisor that misjudges its own reach either offers work it
        # cannot do or talks the user through doing something by hand that
        # it could have done itself.
        message = (f"{clock_block()}\n\n{capability_block(conductor)}\n\n"
                   f"{context}\n\nUser says: {text}")
        await client.query(message)

        reply_parts: list[str] = []
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        reply_parts.append(block.text)
            sid = getattr(msg, "session_id", None)
            if sid and self._store().manager().get("session_id") != sid:
                self._store().set_manager("anthropic", sid)
                conductor.bus.emit(ObservabilityEvent(
                    type="manager.session", component="manager",
                    manager_session_id=sid,
                    data={"prompt_version": MANAGER_PROMPT_VERSION}))

        reply = " ".join(" ".join(reply_parts).split())
        return ManagerTurn(reply=reply, tool_calls=self._turn_calls)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
