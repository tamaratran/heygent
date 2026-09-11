"""The Boss's tools, reachable from outside the process - by the right caller.

When the Boss is a visible Claude Code session, its tools cannot be
in-process functions any more: the session is a separate process, and
it reaches its tools through boss-mcp, a subprocess of the session with
no conductor of its own. This bridge is how boss-mcp gets one: a Unix
socket served here, in conduct.py, where the conductor lives.

    Boss session -> boss-mcp (stdio) -> this socket -> conductor.handle_action

Who is calling is decided by the process, not the model. When the
Conductor writes a Boss session's MCP config it mints a credential and
puts it, with the Boss session id, in boss-mcp's environment. Every
message on this socket carries both; a message with the wrong pair is
refused. The model supplies tool arguments only - never an identity.

The first thing a boss-mcp does is say hello: protocol version and the
tools it registered. The Conductor's readiness gate waits for that hello
and checks it, so a Boss is never marked ready on hope.

Wire protocol, one JSON object per line, both directions:

    -> {"hello": {"protocol": 1, "tools": [...]}, "auth": {...}}
    <- {"ok": true, "protocol": 1}
    -> {"id": "...", "tool": "send_to_task", "args": {...}, "auth": {...}}
    <- {"id": "...", "ok": true, "result": "<text>"}
    <- {"id": "...", "ok": false, "error": "<text>"}
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Callable

from .boss_tools import PROTOCOL_VERSION
from .manager import ToolCall
from .observability import ObservabilityEvent, application_log


class BossToolHost:
    """What serves the Boss its tools, whichever way the calls arrive:
    over this module's Unix socket (boss-mcp) or over the conductor's own
    HTTP endpoint (conductor_mcp). One credential, one caller, one
    execution record per call, the same hooks for the timeline."""

    def __init__(self, conductor, serialize: Callable[[object], str]) -> None:
        self.conductor = conductor
        self.serialize = serialize
        # Tool calls since the last drain: the backend takes them at the
        # end of each turn, so a turn's ToolCalls are exactly the calls
        # the Boss made while answering it.
        self.calls: list[ToolCall] = []
        self.on_call: Callable[[ToolCall], None] | None = None
        # Lifecycle hooks: one execution id from start to finish, so the
        # timeline shows "Searching projects..." become "Found ..." on the
        # same line rather than two unrelated entries.
        self.on_start: Callable[[str, str, dict], None] | None = None
        self.on_finish: Callable[[str, ToolCall, bool], None] | None = None
        # Who may call: set by the backend before it launches the Boss.
        self._boss_id: str = ""
        self._token: str = ""
        # What the last hello said, and the event the gate waits on.
        self.connected: dict | None = None
        self.hello_received = asyncio.Event()

    # -- binding ----------------------------------------------------------
    def expect(self, boss_id: str, token: str) -> None:
        """The one caller this host will serve. A new Boss session, or a
        restarted one, binds again; the old credential stops working."""
        self._boss_id = boss_id
        self._token = token
        self.connected = None
        self.hello_received = asyncio.Event()

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(32)

    def _authorized(self, auth: dict | None) -> bool:
        auth = auth or {}
        return bool(self._token) and \
            hmac.compare_digest(str(auth.get("token", "")), self._token) and \
            str(auth.get("boss_id", "")) == self._boss_id

    def _mark_connected(self, tools: list[str], protocol: int = PROTOCOL_VERSION) -> None:
        self.connected = {"protocol": protocol, "tools": list(tools),
                          "at": time.time()}
        self.hello_received.set()
        self.conductor.bus.emit(ObservabilityEvent(
            type="boss.mcp_connected", component="manager",
            data={"boss_session_id": self._boss_id,
                  "tools": len(self.connected["tools"])}))

    async def wait_connected(self, timeout: float) -> dict | None:
        """The readiness gate: the hello, or None if none came in time."""
        try:
            await asyncio.wait_for(self.hello_received.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        return self.connected

    def drain(self) -> list[ToolCall]:
        calls, self.calls = self.calls, []
        return calls

    async def _handle(self, request: dict) -> dict:
        tool = str(request.get("tool") or "")
        args = request.get("args") or {}
        request_id = request.get("id")
        started = time.monotonic()
        execution_id = "exec_" + secrets.token_hex(4)
        self.conductor.bus.emit(ObservabilityEvent(
            type="boss.tool_call", component="manager",
            task_id=args.get("task_id"), project_id=args.get("project_id"),
            data={"tool": tool, "args": args, "execution_id": execution_id,
                  "boss_session_id": self._boss_id}))
        if self.on_start:
            try:
                self.on_start(execution_id, tool, args)
            except Exception:
                application_log("manager", "boss.on_start_failed",
                                "the tool-start observer failed",
                                severity="warning", exc_info=True)
        try:
            result = await self.conductor.handle_action(tool, args)
            text = self.serialize(result)
            call = ToolCall(tool=tool, args=args, result=text)
            ok, payload = True, {"result": text}
        except Exception as exc:
            application_log("manager", "manager.tool_failed",
                            f"{tool} failed", severity="error", exc_info=True,
                            tool=tool, task_id=args.get("task_id"))
            call = ToolCall(tool=tool, args=args, result=f"error: {exc}")
            ok, payload = False, {"error": str(exc)[:500]}
        self.calls.append(call)
        for hook, hook_args, label in ((self.on_finish, (execution_id, call, ok), "finish"),
                                       (self.on_call, (call,), "call")):
            if hook is None:
                continue
            try:
                hook(*hook_args)
            except Exception:
                application_log("manager", f"boss.on_{label}_failed",
                                f"the tool-{label} observer failed",
                                severity="warning", exc_info=True)
        self.conductor.bus.emit(ObservabilityEvent(
            type="boss.tool_result", component="manager",
            task_id=args.get("task_id"),
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            data={"tool": tool, "ok": ok, "execution_id": execution_id,
                  "result": (payload.get("result") or payload.get("error") or "")[:300]}))
        return {"id": request_id, "ok": ok, **payload}


class BossBridge(BossToolHost):
    def __init__(self, socket_path: str | Path, conductor,
                 serialize: Callable[[object], str]) -> None:
        super().__init__(conductor, serialize)
        self.socket_path = Path(socket_path)
        self._server: asyncio.AbstractServer | None = None

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._serve, path=str(self.socket_path))
        self.socket_path.chmod(0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            self.socket_path.unlink()
        except OSError:
            pass

    # -- serving ------------------------------------------------------------------
    async def _serve(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    request = json.loads(line)
                except ValueError:
                    reply = {"id": None, "ok": False, "error": "not JSON"}
                else:
                    reply = await self._dispatch(request)
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()

    async def _dispatch(self, request: dict) -> dict:
        if not self._authorized(request.get("auth")):
            application_log("manager", "boss.bridge_refused",
                            "a caller without the Boss credential was refused",
                            severity="warning",
                            claimed_boss=str((request.get("auth") or {}).get("boss_id", ""))[:40])
            return {"id": request.get("id"), "ok": False,
                    "error": "not authorized for this Boss session"}
        if "hello" in request:
            return self._hello(request["hello"] or {})
        return await self._handle(request)

    def _hello(self, hello: dict) -> dict:
        protocol = hello.get("protocol")
        if protocol != PROTOCOL_VERSION:
            application_log("manager", "boss.protocol_mismatch",
                            f"boss-mcp speaks protocol {protocol}, "
                            f"the Conductor expects {PROTOCOL_VERSION}",
                            severity="error")
            return {"ok": False,
                    "error": f"Boss tooling incompatible: protocol {protocol} "
                             f"!= {PROTOCOL_VERSION}"}
        self._mark_connected(list(hello.get("tools") or []), protocol)
        return {"ok": True, "protocol": PROTOCOL_VERSION}


# -- client side, used by boss-mcp -------------------------------------------------

async def _exchange(socket_path: str | Path, message: dict,
                    timeout: float) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        writer.write(json.dumps(message).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line:
            return {"ok": False, "error": "bridge closed"}
        return json.loads(line)
    finally:
        writer.close()


async def call_over_socket(socket_path: str | Path, tool: str, args: dict,
                           request_id: str = "1", timeout: float = 600.0,
                           token: str = "", boss_id: str = "") -> dict:
    """One tool call, one reply."""
    return await _exchange(socket_path, {
        "id": request_id, "tool": tool, "args": args,
        "auth": {"token": token, "boss_id": boss_id}}, timeout)


async def hello_over_socket(socket_path: str | Path, token: str, boss_id: str,
                            protocol: int, tools: list[str],
                            timeout: float = 30.0) -> dict:
    """The handshake: who I am, what I speak, what I offer."""
    return await _exchange(socket_path, {
        "hello": {"protocol": protocol, "tools": tools},
        "auth": {"token": token, "boss_id": boss_id}}, timeout)
