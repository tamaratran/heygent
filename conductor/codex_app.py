"""Codex driven through its app-server: our protocol, our pixels.

A CLI in a PTY gives us a screen to scrape and a keyboard to type at,
and the CLI owns every pixel. `codex app-server` is the other way
round: JSON-RPC over stdio, a documented protocol (98 client methods,
79 server notifications, 10 server requests - `codex app-server
generate-json-schema` emits the lot), and no rendering at all. Whoever
speaks it draws the conversation however they like.

That is the point of this module. It is the transport and nothing else:

    thread    a conversation, started or resumed by id
    turn      one exchange; streams items while it runs
    item      a message, a command, a file change, a tool call
    approval  a server REQUEST - Codex asking, and waiting for our answer

so a front end can render turns as cards and answer an approval with a
button instead of a number key.

Nothing here touches the running conductor. The Boss is a Claude Code
session in a PTY (conductor/pty_manager.py) and stays one; this is the
seam a different Boss would be built on, kept separate until it earns
its place.

    app = CodexApp(cwd="/repo")
    await app.start()
    await app.start_thread()
    async for event in app.turn("what changed in PR 81?"):
        ...                      # AgentDelta, ItemDone, TurnDone
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from .observability import application_log

PROTOCOL_CLIENT = "voice-conductor"
# Long enough for a model to think, short enough that a wedged server is
# not mistaken for a slow one.
REQUEST_TIMEOUT_S = 600.0
START_TIMEOUT_S = 60.0


class CodexUnavailable(RuntimeError):
    """No codex app-server to talk to. Never a half-started one."""


# -- what a front end renders ---------------------------------------------------

@dataclass
class Event:
    """One thing that happened in a turn."""
    kind: str                     # delta | item | turn_done | error | approval
    text: str = ""
    item_type: str = ""           # agentMessage, commandExecution, ...
    item_id: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class Approval:
    """Codex asking permission, and waiting. `answer` is how a button
    says yes: it settles the request the server is blocked on."""
    kind: str                     # command | file_change | permissions
    item_id: str
    thread_id: str
    turn_id: str
    question: str
    detail: str
    _answer: Callable[[str], None]
    cwd: str = ""
    answered: bool = False

    # What the protocol accepts, named for what a person would press.
    DECISIONS = ("accept", "acceptForSession", "decline", "cancel")

    def answer(self, decision: str = "accept") -> None:
        if decision not in self.DECISIONS:
            raise ValueError(f"unknown decision: {decision!r}")
        if self.answered:
            return                # a button pressed twice is one answer
        self.answered = True
        self._answer(decision)


class CodexApp:
    """One `codex app-server` process, and the thread we hold in it."""

    APPROVAL_REQUESTS = {
        "item/commandExecution/requestApproval": "command",
        "item/fileChange/requestApproval": "file_change",
        "item/permissions/requestApproval": "permissions",
        "execCommandApproval": "command",
        "applyPatchApproval": "file_change",
    }

    def __init__(self, cwd: str | os.PathLike, binary: str | None = None,
                 config: dict | None = None,
                 on_approval: Callable[[Approval], None] | None = None) -> None:
        self.cwd = str(cwd)
        self.binary = binary or shutil.which("codex") or "codex"
        self.config = dict(config or {})
        # Called the moment Codex asks. A front end holds the Approval,
        # draws its buttons, and answers whenever the user presses one -
        # the server waits, which is what makes a button possible at all.
        self.on_approval = on_approval
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.model: str = ""
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue[Event] | None = None
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._stderr_tail: list[str] = []
        # Approvals sitting unanswered - a human deciding. While one
        # waits, turn() must not time out: ten minutes of server
        # silence is a fault, ten minutes of human thought is normal.
        # Measured on 2026-08-30: the demo crashed with TimeoutError
        # at its own permission card, buttons still on screen.
        self._awaiting = 0

    # -- the process ---------------------------------------------------------
    async def start(self) -> None:
        """Spawn the server and shake hands. Raises rather than leaving a
        half-started one behind."""
        argv = [self.binary, "app-server"]
        for key, value in self.config.items():
            argv += ["-c", f"{key}={json.dumps(value)}"]
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=self.cwd)
        except OSError as exc:
            raise CodexUnavailable(f"could not start {self.binary}: {exc}")
        self._events = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr = asyncio.create_task(self._read_stderr())
        try:
            hello = await self._request(
                "initialize",
                {"clientInfo": {"name": PROTOCOL_CLIENT, "version": "0.1"}},
                timeout=START_TIMEOUT_S)
        except Exception as exc:
            await self.stop()
            raise CodexUnavailable(
                f"codex app-server did not answer initialize: {exc}"
                + (f" ({' '.join(self._stderr_tail)[:200]})"
                   if self._stderr_tail else ""))
        self._notify("initialized", {})
        application_log("manager", "codex.app_server_started",
                        f"codex app-server is up in {self.cwd}",
                        severity="debug",
                        codex_home=str(hello.get("codexHome", "")))

    async def stop(self) -> None:
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
        self._reader = self._stderr = None
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                process.kill()
            except ProcessLookupError:
                pass

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # -- threads and turns ---------------------------------------------------
    async def start_thread(self, cwd: str | None = None,
                           approval_policy: str | None = None,
                           sandbox: dict | None = None, **overrides) -> str:
        """approval_policy is Codex's AskForApproval: "untrusted" asks
        before anything outside the sandbox, "on-request" lets the model
        decide, "never" never asks. Asking is what a button answers, so
        a front end that draws buttons wants one of the first two."""
        params: dict[str, Any] = {"cwd": cwd or self.cwd, **overrides}
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if sandbox:
            params["sandboxPolicy"] = sandbox
        reply = await self._request("thread/start", params)
        self.thread_id = (reply.get("thread") or {}).get("id") \
            or reply.get("threadId")
        self.model = reply.get("model") or ""
        if not self.thread_id:
            raise CodexUnavailable("thread/start named no thread")
        return self.thread_id

    async def resume_thread(self, thread_id: str) -> str:
        reply = await self._request("thread/resume", {"threadId": thread_id})
        self.thread_id = (reply.get("thread") or {}).get("id") or thread_id
        self.model = reply.get("model") or self.model
        return self.thread_id

    async def turn(self, text: str, **overrides) -> AsyncIterator[Event]:
        """One exchange, streamed. Yields deltas as they arrive, each
        finished item, and finally the turn."""
        if not self.thread_id:
            raise CodexUnavailable("no thread; call start_thread first")
        while self._events and not self._events.empty():
            self._events.get_nowait()            # last turn's tail
        self._awaiting = 0                       # and its dead approvals
        params = {"threadId": self.thread_id,
                  "input": [{"type": "text", "text": text}], **overrides}
        await self._request("turn/start", params)
        while True:
            if self._awaiting:
                # Codex is blocked on a human decision, so wait the way
                # the human does: indefinitely. A dead server still ends
                # this - _read_loop posts an error event on EOF.
                event = await self._events.get()
            else:
                event = await asyncio.wait_for(self._events.get(),
                                               REQUEST_TIMEOUT_S)
            yield event
            if event.kind in ("turn_done", "error"):
                return

    async def interrupt(self) -> None:
        """Stop the running turn. The server names the turn in
        turn/started and wants it named back: turn/interrupt without a
        turnId is an invalid request, not a broad "stop whatever"."""
        if self.thread_id and self.turn_id:
            await self._request("turn/interrupt",
                                {"threadId": self.thread_id,
                                 "turnId": self.turn_id})

    # -- the wire ------------------------------------------------------------
    def _write(self, message: dict) -> None:
        if self._process is None or self._process.stdin is None:
            raise CodexUnavailable("codex app-server is not running")
        self._process.stdin.write((json.dumps(message) + "\n").encode())

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(self, method: str, params: dict,
                       timeout: float = REQUEST_TIMEOUT_S) -> dict:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._write({"jsonrpc": "2.0", "id": request_id,
                     "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def _read_stderr(self) -> None:
        assert self._process and self._process.stderr
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            self._stderr_tail = (self._stderr_tail + [text])[-5:]

    async def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        while True:
            line = await self._process.stdout.readline()
            if not line:
                await self._put(Event(kind="error",
                                      text="codex app-server closed"))
                return
            try:
                message = json.loads(line)
            except ValueError:
                continue                       # not ours; the log has it
            try:
                self._dispatch(message)
            except Exception:
                application_log("manager", "codex.dispatch_failed",
                                "could not handle an app-server message",
                                severity="warning", exc_info=True)

    def _dispatch(self, message: dict) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if request_id is not None and method is None:
            future = self._pending.get(request_id)
            if future is None or future.done():
                return
            if "error" in message:
                future.set_exception(RuntimeError(
                    json.dumps(message["error"])[:300]))
            else:
                future.set_result(message.get("result") or {})
            return
        if method is None:
            return
        if request_id is not None:
            self._server_request(request_id, method, message.get("params") or {})
            return
        self._notification(method, message.get("params") or {})

    # -- what the server says ------------------------------------------------
    def _notification(self, method: str, params: dict) -> None:
        if method == "turn/started":
            self.turn_id = (params.get("turn") or {}).get("id") \
                or params.get("turnId") or self.turn_id
        elif method == "item/agentMessage/delta":
            self._put_nowait(Event(kind="delta", text=params.get("delta") or "",
                                   item_id=params.get("itemId") or ""))
        elif method in ("item/completed", "item/started"):
            item = params.get("item") or {}
            if method == "item/started":
                return                        # the finished item is the news
            self._put_nowait(Event(
                kind="item", item_type=item.get("type") or "",
                item_id=item.get("id") or "", text=_item_text(item), data=item))
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            self._put_nowait(Event(kind="turn_done", data=turn,
                                   text=_final_text(turn)))
        elif method == "error":
            self._put_nowait(Event(kind="error",
                                   text=str(params.get("message") or params)))

    def _server_request(self, request_id, method: str, params: dict) -> None:
        """Codex asking us something and waiting for the answer."""
        kind = self.APPROVAL_REQUESTS.get(method)
        if kind is None:
            # Anything we do not implement is refused, not left hanging:
            # a turn blocked on an unanswered request never ends.
            self._write({"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": -32601,
                                   "message": f"{method} is not handled"}})
            return
        answered = {"done": False}
        self._awaiting += 1                    # turn() now waits, untimed

        def answer(decision: str) -> None:
            if answered["done"]:
                return
            answered["done"] = True
            self._awaiting = max(0, self._awaiting - 1)
            self._write({"jsonrpc": "2.0", "id": request_id,
                         "result": {"decision": decision}})

        approval = Approval(
            kind=kind, item_id=params.get("itemId") or "",
            thread_id=params.get("threadId") or "",
            turn_id=params.get("turnId") or "",
            question=_approval_question(kind, params),
            detail=" ".join(str(params.get("reason") or "").split()),
            cwd=str(params.get("cwd") or ""),
            _answer=answer)
        self._put_nowait(Event(kind="approval", text=approval.question,
                               item_id=approval.item_id,
                               data={"approval": approval}))
        if self.on_approval is not None:
            try:
                self.on_approval(approval)
            except Exception:
                application_log("manager", "codex.approval_handler_failed",
                                "the approval handler raised; declining so "
                                "the turn is not stuck", severity="warning",
                                exc_info=True)
                approval.answer("decline")

    # -- the queue -----------------------------------------------------------
    def _put_nowait(self, event: Event) -> None:
        if self._events is not None:
            self._events.put_nowait(event)

    async def _put(self, event: Event) -> None:
        if self._events is not None:
            await self._events.put(event)


def _item_text(item: dict) -> str:
    if item.get("type") == "agentMessage":
        return item.get("text") or ""
    if item.get("type") == "commandExecution":
        return " ".join(str(item.get("command") or "").split())
    if item.get("type") == "fileChange":
        changes = item.get("changes") or []
        return ", ".join(str(c.get("path") or "") for c in changes[:4])
    if item.get("type") == "userMessage":
        return " ".join(part.get("text", "")
                        for part in (item.get("content") or [])
                        if isinstance(part, dict))
    return " ".join(str(item.get("title") or item.get("name") or "").split())


def _final_text(turn: dict) -> str:
    for item in reversed(turn.get("items") or []):
        if item.get("type") == "agentMessage" and item.get("text"):
            return item["text"]
    return ""


def _approval_question(kind: str, params: dict) -> str:
    if kind == "command":
        return " ".join(str(params.get("command") or "a command").split())
    if kind == "file_change":
        return f"edit files under {params.get('grantRoot') or 'this checkout'}"
    return "grant permissions"
