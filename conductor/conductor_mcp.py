"""The Boss's tools, served by the process that has them.

boss-mcp (boss_mcp.py) is a stdio server that Claude Code starts as a
child of the Boss session and that forwards every call over a Unix
socket to the conductor. It works, and it is one more process with a
runtime, a launcher and a lifecycle of its own. The conductor is already
running whenever there is a Boss to serve; this module has it answer MCP
directly, over streamable HTTP on the loopback interface:

    Boss session --HTTP, loopback, bearer credential--> this --> handle_action

Nothing to install, nothing to launch, nothing for Claude Code to
restart when it dies. The MCP SDK, starlette and uvicorn are already
in the app's runtime (claude-agent-sdk brings them), so the endpoint is
in-process code, not a helper.

The endpoint is not a product surface. The URL exists only in the Boss
session's private MCP config; the timeline names the tools' namespace
("Agent Control"), never the port.

Who is calling is decided by the connection, not the model. The
Conductor mints a credential when it writes the Boss session's MCP
config and puts it in the config's Authorization header; a request
without the current credential is refused before it reaches a tool. The
model supplies tool arguments only - never an identity.

Readiness: the session's `tools/list` request, with the right credential,
is the moment the Boss's tools are known to be reachable from the Boss's
process - the gate PtyManagerBackend waits on (CONNECTED_ON says why not
`initialize`).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
from pathlib import Path
from typing import Callable

from .boss_bridge import BossToolHost
from .boss_tools import DESCRIPTIONS, OPTIONAL, SCHEMAS, SERVER_NAME
from .observability import application_log

HOST = "127.0.0.1"
PATH = "/mcp"
# The request that proves the Boss's process has our tools. The MCP SDK's
# own client sends `initialize` first; Claude Code 2.1.250's runtime
# ("mcp runtime arm: v2") never does - measured, its first requests are
# server/discover, subscriptions/listen, prompts/list, resources/list,
# tools/list. The tool listing is the readiness signal that both send,
# and it is the better one: after it, the session knows the tools.
CONNECTED_ON = ("initialize", "tools/list")


class ConductorMcp(BossToolHost):
    """Streamable-HTTP MCP endpoint hosted by the conductor process."""

    def __init__(self, conductor, serialize: Callable[[object], str],
                 home: str | Path, names: tuple[str, ...] | None = None,
                 port: int | None = None, stateless: bool = True) -> None:
        super().__init__(conductor, serialize)
        self.home = Path(home)
        self.names = tuple(names or tuple(SCHEMAS))
        self.port = port
        # Stateless: every request stands alone, so a conductor restart is
        # invisible to a client that keeps the same URL and credential.
        # Stateful (the SDK default) gives each client an Mcp-Session-Id
        # the next conductor process will not recognise.
        self.stateless = stateless
        self._server = None            # uvicorn.Server
        self._task: asyncio.Task | None = None
        self._sock: socket.socket | None = None
        self.requests: list[str] = []  # JSON-RPC methods seen, for the log

    # -- addressing -----------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}{PATH}"

    def _port_file(self) -> Path:
        return self.home / "boss" / "mcp.port"

    def _bind(self) -> socket.socket:
        """The port is remembered across runs: a Boss session launched by
        the previous conductor holds the old URL in its config and must
        find the new conductor at the same place."""
        wanted = self.port
        if wanted is None:
            try:
                wanted = int(self._port_file().read_text().strip())
            except (OSError, ValueError):
                wanted = None
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, wanted or 0))
        except OSError:
            if wanted is None:
                raise
            application_log("manager", "boss.mcp_port_taken",
                            f"port {wanted} is in use; choosing another",
                            severity="warning")
            sock.bind((HOST, 0))
        sock.listen(16)
        self.port = sock.getsockname()[1]
        self._port_file().parent.mkdir(parents=True, exist_ok=True)
        self._port_file().write_text(str(self.port))
        return sock

    # -- the server -------------------------------------------------------------
    def _handler(self, name: str, schema: dict):
        """A coroutine with a real signature, so MCPServer derives the
        input schema from it - the same trick boss-mcp uses."""
        async def handler(**kwargs):
            reply = await self._handle({"tool": name, "args": kwargs})
            if not reply.get("ok"):
                # A ToolError's message reaches the model; any other
                # exception is masked by the SDK as "Error executing tool".
                from mcp.server.mcpserver.exceptions import ToolError
                raise ToolError(reply.get("error") or "tool failed")
            return reply.get("result", "ok")

        optional = OPTIONAL.get(name, {})
        params = [inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY,
                                    annotation=kind,
                                    default=optional.get(
                                        key, inspect.Parameter.empty))
                  for key, kind in schema.items()]
        handler.__signature__ = inspect.Signature(params)
        handler.__annotations__ = dict(schema)
        handler.__name__ = handler.__qualname__ = name
        return handler

    def build_app(self):
        from mcp.server.mcpserver import MCPServer
        server = MCPServer(SERVER_NAME)
        for name in self.names:
            server.tool(name=name, description=DESCRIPTIONS.get(name, name))(
                self._handler(name, SCHEMAS[name]))
        inner = server.streamable_http_app(streamable_http_path=PATH,
                                           stateless_http=self.stateless,
                                           host=HOST)
        return self._guard(inner)

    def _guard(self, inner):
        """Pure-ASGI wrapper: the credential check, and the readiness
        signal. Runs before the SDK sees a byte of the request."""
        async def app(scope, receive, send):
            if scope["type"] != "http":
                return await inner(scope, receive, send)
            headers = {k.decode().lower(): v.decode(errors="replace")
                       for k, v in scope.get("headers") or []}
            token = ""
            auth = headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            if not self._authorized({"token": token, "boss_id": self._boss_id}):
                application_log("manager", "boss.mcp_refused",
                                "a request without the Boss credential was refused",
                                severity="warning", path=scope.get("path", ""))
                body = json.dumps({"error": "not authorized for this Boss session"}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            # Read the body once to see the JSON-RPC method, then hand the
            # same messages to the SDK.
            messages, body = [], b""
            while True:
                message = await receive()
                messages.append(message)
                body += message.get("body", b"")
                if not message.get("more_body") or message["type"] != "http.request":
                    break
            method = ""
            try:
                method = str(json.loads(body or b"{}").get("method", ""))
            except (ValueError, AttributeError):
                pass
            if method:
                self.requests.append(method)
            if method in CONNECTED_ON:
                self._mark_connected(list(self.names))

            async def replay():
                if messages:
                    return messages.pop(0)
                return await receive()
            await inner(scope, replay, send)
        return app

    async def start(self) -> None:
        import uvicorn
        self._sock = self._bind()
        config = uvicorn.Config(self.build_app(), host=HOST, port=self.port,
                                log_level="warning", lifespan="on",
                                access_log=False)
        self._server = uvicorn.Server(config)
        # Server.serve() installs its own SIGINT/SIGTERM handlers with
        # signal.signal, over the conductor's. _serve() is the same
        # without that; fall back to serve() if a future uvicorn renames it.
        serve = getattr(self._server, "_serve", None) or self._server.serve
        self._task = asyncio.create_task(serve(sockets=[self._sock]))
        # The port is bound already; the task is the request loop.
        await asyncio.sleep(0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, 10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
            self._task = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._server = None

    # -- what a Boss session's config says -----------------------------------------
    def mcp_config_entry(self, credential: str) -> dict:
        return {"type": "http", "url": self.url,
                "headers": {"Authorization": f"Bearer {credential}"}}
