"""ClaudeApp: the Boss page's Claude Code backend.

The same face CodexApp shows CodexWeb - start/stop, start_thread and
resume_thread, a turn() that streams Events, interrupt() - but held up
by a ClaudeSDKClient instead of a codex app-server, so the Boss behind
the page is literally Claude Code. Claude messages are translated into
the Event vocabulary the page already draws: assistant text arrives as
deltas, tool uses as activity items, permission requests as Approval
cards the buttons settle.
"""

from __future__ import annotations

import asyncio
import secrets

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                              ClaudeSDKClient, PermissionResultAllow,
                              PermissionResultDeny, ResultMessage,
                              SystemMessage, TextBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage)

from .codex_app import Approval, Event
from .observability import application_log

# Everyday reads and edits go without asking; anything else (Bash first
# of all) stops at an approval card, the stance the terminal Boss takes.
PREAPPROVED = ["Read", "Glob", "Grep", "Edit", "Write"]


def _gist(name: str, args: dict) -> str:
    for key in ("command", "file_path", "pattern", "path", "url", "prompt"):
        if isinstance(args.get(key), str):
            return f"{name}({' '.join(args[key].split())[:70]})"
    return name


class ClaudeUnavailable(RuntimeError):
    """No Claude Code to talk to. Never a half-started one."""


class ClaudeApp:
    """One persistent Claude Code session, spoken to like CodexApp."""

    CUT_AFTER = 5.0   # how long an interrupt waits for a ResultMessage

    def __init__(self, cwd: str, on_approval=None) -> None:
        self.cwd = str(cwd)
        self.on_approval = on_approval
        self.thread_id: str | None = None
        self.model: str = "claude-code"
        self._client: ClaudeSDKClient | None = None
        self._events: asyncio.Queue[Event] | None = None
        self._reader: asyncio.Task | None = None
        self._session_tools: set[str] = set()   # "don't ask again" answers
        self._turn_text: list[str] = []
        self._cut: asyncio.Task | None = None
        self._commands: dict[str, str] = {}   # tool_use id -> command line

    # -- the process -------------------------------------------------------
    async def start(self) -> None:
        """Nothing to spawn up front: a client is connected per thread."""

    async def stop(self) -> None:
        await self._disconnect()

    async def _disconnect(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        if self._client is not None:
            client, self._client = self._client, None
            try:
                await client.disconnect()
            except Exception:
                application_log("ui", "claude_app.disconnect_failed",
                                "the Claude client would not disconnect",
                                severity="warning", exc_info=True)

    # -- threads and turns ---------------------------------------------------
    async def start_thread(self, **_ignored) -> str:
        """A fresh conversation. Codex-specific options (sandbox,
        approval policy) are accepted and ignored: Claude's stance is
        set by PREAPPROVED and the approval callback."""
        await self._connect(resume=None)
        return self.thread_id or ""

    async def resume_thread(self, thread_id: str) -> str:
        await self._connect(resume=thread_id)
        self.thread_id = thread_id
        return thread_id

    async def _connect(self, resume: str | None) -> None:
        await self._disconnect()
        self.thread_id = resume
        self._session_tools.clear()
        options = ClaudeAgentOptions(
            cwd=self.cwd,
            allowed_tools=list(PREAPPROVED),
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            resume=resume,
        )
        client = ClaudeSDKClient(options)
        try:
            await client.connect()
        except Exception as exc:
            raise ClaudeUnavailable(str(exc)) from exc
        self._client = client
        self._events = asyncio.Queue()
        self._reader = asyncio.create_task(self._read())

    async def turn(self, text: str, **_ignored):
        """One exchange, streamed: deltas, activity items, approvals,
        then turn_done. The reader feeds the queue; this drains it."""
        if self._client is None:
            await self._connect(resume=self.thread_id)
        self._turn_text.clear()
        await self._client.query(text)
        while True:
            event = await self._events.get()
            yield event
            if event.kind in ("turn_done", "error"):
                return

    async def interrupt(self) -> None:
        """Cut the turn short. Claude Code does not always answer an
        interrupt with a ResultMessage, so a watchdog settles the turn
        if nothing arrives; a result that does arrive disarms it."""
        if self._client is None:
            return
        await self._client.interrupt()

        async def settle() -> None:
            await asyncio.sleep(self.CUT_AFTER)
            said = "\n\n".join(t.strip() for t in self._turn_text
                               if t.strip())
            self._turn_text.clear()
            self._put(Event(kind="error",
                            text=said or "Interrupted by user"))

        self._disarm()
        self._cut = asyncio.create_task(settle())

    def _disarm(self) -> None:
        if self._cut is not None:
            self._cut.cancel()
            self._cut = None

    # -- translation ---------------------------------------------------------
    def _put(self, event: Event) -> None:
        if self._events is not None:
            self._events.put_nowait(event)

    async def _read(self) -> None:
        """Translate the Claude message stream into page Events."""
        try:
            async for message in self._client.receive_messages():
                sid = getattr(message, "session_id", None)
                if sid is None and isinstance(message, SystemMessage):
                    sid = (message.data or {}).get("session_id")
                if sid:
                    self.thread_id = sid
                if isinstance(message, SystemMessage):
                    model = (message.data or {}).get("model")
                    if model:
                        self.model = str(model)
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            self._turn_text.append(block.text)
                            self._put(Event(kind="delta",
                                            text=block.text + "\n\n"))
                        elif isinstance(block, ToolUseBlock):
                            args = block.input or {}
                            if block.name == "Bash" and \
                                    isinstance(args.get("command"), str):
                                self._commands[block.id or ""] = \
                                    " ".join(args["command"].split())
                            else:
                                self._put(self._tool_item(block))
                elif isinstance(message, UserMessage):
                    content = message.content
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, ToolResultBlock):
                                self._tool_result(block)
                elif isinstance(message, ResultMessage):
                    self._disarm()
                    said = "\n\n".join(t.strip() for t in self._turn_text
                                       if t.strip())
                    self._turn_text.clear()
                    if message.is_error:
                        self._put(Event(kind="error",
                                        text=said or message.subtype))
                    else:
                        self._put(Event(kind="turn_done", text=said))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            application_log("ui", "claude_app.reader_failed",
                            "the Claude reader failed", severity="error",
                            exc_info=True)
            self._put(Event(kind="error", text=str(exc)[:300]))

    def _tool_result(self, block: ToolResultBlock) -> None:
        """A command's row is drawn when its result lands, the way codex
        reports completed items, so the row can open into the output."""
        command = self._commands.pop(block.tool_use_id, None)
        if command is None:
            return
        content = block.content
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", ""))
                                for part in content
                                if isinstance(part, dict))
        data = {"output": str(content or "")}
        if block.is_error:
            data["exitCode"] = 1
        self._put(Event(kind="item", item_type="commandExecution",
                        item_id=block.tool_use_id, text=command,
                        data=data))

    @staticmethod
    def _tool_item(block: ToolUseBlock) -> Event:
        args = block.input or {}
        if block.name in ("Edit", "Write", "MultiEdit", "NotebookEdit") \
                and isinstance(args.get("file_path"), str):
            return Event(kind="item", item_type="fileChange",
                         item_id=block.id or "", text=args["file_path"],
                         data={"changes": [{"kind": "update",
                                            "path": args["file_path"]}]})
        return Event(kind="item", item_type="mcpToolCall",
                     item_id=block.id or "",
                     text=_gist(block.name, args))

    async def _can_use_tool(self, tool_name: str, args: dict, context):
        """Anything not pre-approved stops here, becomes a card, and
        waits for the button - the session itself stays alive."""
        if tool_name in self._session_tools:
            return PermissionResultAllow()
        future = asyncio.get_running_loop().create_future()

        def answer(decision: str) -> None:
            if not future.done():
                future.set_result(decision)

        approval = Approval(
            kind="command" if tool_name == "Bash" else "permissions",
            item_id="appr_" + secrets.token_hex(4),
            thread_id=self.thread_id or "", turn_id="",
            question=f"Allow {_gist(tool_name, args)}?",
            detail=_gist(tool_name, args), cwd=self.cwd, _answer=answer)
        self._put(Event(kind="approval", text=approval.question,
                        item_id=approval.item_id,
                        data={"approval": approval}))
        if self.on_approval is not None:
            self.on_approval(approval)
        decision = await future
        if decision in ("accept", "acceptForSession"):
            if decision == "acceptForSession":
                self._session_tools.add(tool_name)
            return PermissionResultAllow()
        return PermissionResultDeny(message="The user denied this action.")
