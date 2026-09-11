"""The conductor answers MCP itself, on loopback, to the right caller.

These tests use the MCP SDK's own streamable-HTTP client against the
endpoint conductor_mcp serves: the same protocol Claude Code speaks. A
request without the Boss's credential is refused before the SDK sees
it; with it, the tools are listed and calls reach handle_action with
the caller's identity supplied by the connection, never by an argument.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor.boss_tools import REQUIRED_TOOLS
from conductor.conductor_mcp import ConductorMcp
from conductor.pty_manager import PtyManagerBackend


class FakeConductor:
    def __init__(self):
        self.bus = mock.Mock()
        self.actions = []

    async def handle_action(self, tool, args):
        self.actions.append((tool, args))
        if tool == "inspect_project":
            raise RuntimeError("no such project")
        return {"echo": args}


def run(coro):
    return asyncio.run(coro)


async def talk(url: str, token: str, do):
    """Open the SDK's HTTP client on the endpoint and hand the session
    to `do`."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import (create_mcp_http_client,
                                            streamable_http_client)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with create_mcp_http_client(headers=headers) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await do(session)


class TheEndpointServesItsBoss(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.conductor = FakeConductor()

    def tearDown(self):
        self.tmp.cleanup()

    def endpoint(self, **kw) -> ConductorMcp:
        return ConductorMcp(self.conductor, json.dumps, self.home,
                            names=("search_sessions", "inspect_project"), **kw)

    def test_the_tools_are_listed_and_called_with_the_credential(self):
        mcp = self.endpoint()

        async def scenario():
            await mcp.start()
            token = mcp.new_token()
            mcp.expect("boss_1", token)
            try:
                async def do(session):
                    tools = await session.list_tools()
                    result = await session.call_tool("search_sessions",
                                                     {"query": "posely"})
                    return [t.name for t in tools.tools], result
                names, result = await talk(mcp.url, token, do)
                connected = await mcp.wait_connected(1)
            finally:
                await mcp.stop()
            return names, result, connected
        names, result, connected = run(scenario())
        self.assertEqual(names, ["search_sessions", "inspect_project"])
        self.assertEqual(self.conductor.actions,
                         [("search_sessions", {"query": "posely"})])
        self.assertIn('"query": "posely"', result.content[0].text)
        self.assertIsNotNone(connected, "initialize marks the Boss connected")
        self.assertIn("search_sessions", connected["tools"])
        self.assertEqual(mcp.drain()[0].tool, "search_sessions")

    def test_without_the_credential_nothing_is_served(self):
        async def scenario():
            mcp = self.endpoint()
            await mcp.start()
            mcp.expect("boss_1", mcp.new_token())
            try:
                for token in ("", "wrong"):
                    with self.assertRaises(Exception):
                        await talk(mcp.url, token, lambda s: s.list_tools())
                connected = await mcp.wait_connected(0.2)
            finally:
                await mcp.stop()
            return connected
        self.assertIsNone(run(scenario()))
        self.assertEqual(self.conductor.actions, [])

    def test_a_new_credential_retires_the_old_one(self):
        async def scenario():
            mcp = self.endpoint()
            await mcp.start()
            old = mcp.new_token()
            mcp.expect("boss_1", old)
            mcp.expect("boss_2", mcp.new_token())
            try:
                with self.assertRaises(Exception):
                    await talk(mcp.url, old, lambda s: s.list_tools())
            finally:
                await mcp.stop()
        run(scenario())

    def test_a_failing_tool_is_an_error_not_a_result(self):
        async def scenario():
            mcp = self.endpoint()
            await mcp.start()
            token = mcp.new_token()
            mcp.expect("boss_1", token)
            try:
                return await talk(mcp.url, token,
                                  lambda s: s.call_tool("inspect_project",
                                                        {"project_id": "p"}))
            finally:
                await mcp.stop()
        result = run(scenario())
        self.assertTrue(result.is_error)
        self.assertIn("no such project", result.content[0].text)

    def test_the_port_is_remembered_and_reused(self):
        async def scenario():
            first = self.endpoint()
            await first.start()
            port = first.port
            await first.stop()
            second = self.endpoint()
            await second.start()
            again = second.port
            await second.stop()
            return port, again
        port, again = run(scenario())
        self.assertEqual(port, again)
        self.assertEqual((self.home / "boss" / "mcp.port").read_text(), str(port))

    def test_the_boss_survives_the_endpoint_going_away_and_back(self):
        """Stateless: a client that keeps its URL and credential does not
        notice that a different process is answering now."""
        async def scenario():
            mcp = self.endpoint()
            await mcp.start()
            token = mcp.new_token()
            mcp.expect("boss_1", token)
            url = mcp.url
            await talk(url, token, lambda s: s.list_tools())
            await mcp.stop()
            again = self.endpoint()
            await again.start()
            again.expect("boss_1", token)
            try:
                result = await talk(url, token,
                                    lambda s: s.call_tool("search_sessions", {"query": "x"}))
            finally:
                await again.stop()
            return url == again.url, result
        same, result = run(scenario())
        self.assertTrue(same)
        self.assertFalse(result.is_error)


class TheBossIsPointedAtTheEndpoint(unittest.TestCase):
    """PtyManagerBackend with transport="http": no helper, the session's
    config names the conductor's URL with the credential in a header."""

    def test_the_config_is_http_with_the_credential_in_a_header(self):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        backend = PtyManagerBackend(mock.Mock(), home, home / "x.sock",
                                    python="py", repo_root=home,
                                    transport="http")
        mcp = ConductorMcp(FakeConductor(), json.dumps, home, port=43127)
        mcp.port = 43127
        backend.attach_bridge(mcp)
        backend.boss_dir.mkdir(parents=True)
        spec = backend.boss_spec(None, ("search_sessions",), "boss_9", "tok",
                                 "sid", resume=False)
        argv = backend.adapter.boss_argv(spec)
        path = backend.boss_dir / "mcp.json"
        self.assertIn(str(path), argv)
        entry = json.loads(path.read_text())["mcpServers"]["boss"]
        self.assertEqual(entry["type"], "http")
        self.assertEqual(entry["url"], "http://127.0.0.1:43127/mcp")
        self.assertEqual(entry["headers"]["Authorization"], "Bearer tok")
        self.assertNotIn("command", entry)
        self.assertNotIn("boss_9", json.dumps(entry),
                         "the Boss id is the connection's, never a parameter")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        tmp.cleanup()

    def test_without_a_listening_endpoint_no_boss_is_launched(self):
        """The gate bites before the session exists: an endpoint that is
        not listening means no Boss, not a Boss without tools - and no
        duplicate later."""
        from conductor.pty_manager import BossUnavailable
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        runtime = mock.Mock()
        backend = PtyManagerBackend(runtime, home, home / "x.sock",
                                    python="py", repo_root=home,
                                    transport="http")
        backend.attach_bridge(ConductorMcp(FakeConductor(), json.dumps, home))
        conductor = FakeConductor()
        conductor.projects = mock.Mock()
        conductor.projects.manager.return_value = {"session_id": None}
        conductor.MANAGER_TOOLS = tuple(REQUIRED_TOOLS)
        with self.assertRaises(BossUnavailable):
            asyncio.run(backend._ensure_session(conductor))
        runtime.launch_session.assert_not_called()
        tmp.cleanup()

    def test_a_live_boss_is_not_relaunched_on_the_next_turn(self):
        """The runtimes report status as a string. Measured: reading
        `.status` off it made every Boss look dead, and every turn killed
        and resumed the session."""
        runtime = mock.Mock()
        backend = PtyManagerBackend(runtime, "/tmp/x", "/tmp/x.sock",
                                    python="py", repo_root="/tmp")
        for state, alive in (("idle", True), ("working", True),
                             ("disconnected", False), ("failed", False)):
            runtime.get_status = mock.AsyncMock(return_value=state)
            self.assertEqual(asyncio.run(backend._alive("sid")), alive, state)
        record = mock.Mock(status="idle")
        runtime.get_status = mock.AsyncMock(return_value=record)
        self.assertTrue(asyncio.run(backend._alive("sid")))

    def test_an_unknown_transport_is_refused(self):
        with self.assertRaises(ValueError):
            PtyManagerBackend(mock.Mock(), "/tmp/x", "/tmp/x.sock",
                              python="py", repo_root="/tmp", transport="carrier pigeon")

    def test_required_tools_are_what_the_endpoint_serves(self):
        mcp = ConductorMcp(FakeConductor(), json.dumps, "/tmp/x")
        for tool in REQUIRED_TOOLS:
            self.assertIn(tool, mcp.names)


if __name__ == "__main__":
    unittest.main()
