"""boss-mcp: the Boss's orchestration tools, as an MCP server.

Started BY Claude Code, as the MCP server named "boss" in the Boss
session's MCP config. It has no conductor of its own and holds no state;
every call is forwarded over the bridge socket to conduct.py, where the
conductor is. It is an adapter, disposable: if it dies, Claude Code
restarts it, and nothing about the Boss, its timeline or its workers has
moved.

    Boss session --stdio--> this --authenticated unix socket--> BossBridge

Identity is the process's, not the model's. The credential and the Boss
session id arrive in this process's environment, put there by the
Conductor when it wrote the session's MCP config; the model supplies tool
arguments only. On start the server announces itself to the bridge -
protocol version, the tools it registered - and the Conductor will not
call the Boss ready until it has.

    boss-mcp --version                      protocol version, for the gate
    boss-mcp --socket <path> --list         the tools it would offer
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys

from .boss_tools import (DESCRIPTIONS, OPTIONAL, PROTOCOL_VERSION, SCHEMAS,
                         SERVER_NAME)

ENV_TOKEN = "BOSS_MCP_TOKEN"
ENV_SESSION = "BOSS_SESSION_ID"


def _handler(name: str, schema: dict, socket_path: str, token: str,
             boss_id: str):
    """A coroutine with a real signature, so MCPServer derives the input
    schema from it exactly as the SDK backend's `tool()` did."""
    from .boss_bridge import call_over_socket

    async def handler(**kwargs):
        reply = await call_over_socket(socket_path, name, kwargs,
                                       token=token, boss_id=boss_id)
        if not reply.get("ok"):
            # The reason IS the answer: an exception raised here reaches
            # the model as a bare "Error executing tool <name>", and a
            # Boss that cannot read why a call failed cannot tell the
            # user what to do about it.
            return f"error: {reply.get('error') or 'tool failed'}"
        return reply.get("result", "ok")

    optional = OPTIONAL.get(name, {})
    params = [inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY,
                                annotation=kind,
                                default=optional.get(
                                    key, inspect.Parameter.empty))
              for key, kind in schema.items()]
    handler.__signature__ = inspect.Signature(params)
    handler.__annotations__ = dict(schema)
    handler.__name__ = name
    handler.__qualname__ = name
    return handler


def build_server(socket_path: str, names: tuple[str, ...] | None = None,
                 token: str = "", boss_id: str = ""):
    from mcp.server.mcpserver import MCPServer
    server = MCPServer(SERVER_NAME)
    for name in (names or tuple(SCHEMAS)):
        server.tool(name=name, description=DESCRIPTIONS.get(name, name))(
            _handler(name, SCHEMAS[name], socket_path, token, boss_id))
    return server


async def announce(socket_path: str, names: tuple[str, ...], token: str,
                   boss_id: str) -> bool:
    """Tell the Conductor this server is up and what it offers. The
    Conductor's readiness gate waits for exactly this."""
    from .boss_bridge import hello_over_socket
    try:
        reply = await hello_over_socket(socket_path, token=token,
                                        boss_id=boss_id,
                                        protocol=PROTOCOL_VERSION,
                                        tools=list(names))
    except OSError as exc:
        print(f"boss-mcp: no Conductor at {socket_path}: {exc}",
              file=sys.stderr, flush=True)
        return False
    if not reply.get("ok"):
        print(f"boss-mcp: refused by the Conductor: {reply.get('error')}",
              file=sys.stderr, flush=True)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", help="the BossBridge socket conduct.py serves")
    parser.add_argument("--tools", default="",
                        help="comma-separated subset of tool names")
    parser.add_argument("--list", action="store_true",
                        help="print the tools and exit")
    parser.add_argument("--version", action="store_true",
                        help="print the protocol version and exit")
    args = parser.parse_args(argv)
    if args.version:
        print(f"boss-mcp protocol {PROTOCOL_VERSION}")
        return 0
    if not args.socket:
        parser.error("--socket is required")
    names = tuple(n for n in args.tools.split(",") if n) or tuple(SCHEMAS)
    token = os.environ.get(ENV_TOKEN, "")
    boss_id = os.environ.get(ENV_SESSION, "")
    server = build_server(args.socket, names, token, boss_id)
    if args.list:
        for tool in asyncio.run(server.list_tools()):
            print(json.dumps({"name": tool.name,
                              "required": tool.input_schema.get("required", [])}))
        return 0
    if not asyncio.run(announce(args.socket, names, token, boss_id)):
        # Refused or unreachable: better to exit so Claude Code reports a
        # failed MCP server than to serve tools that cannot reach anything.
        return 3
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
