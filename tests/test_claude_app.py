"""ClaudeApp: Claude Code behind the Boss page, speaking CodexApp's Events."""

import asyncio
import inspect
import unittest

from claude_agent_sdk import (AssistantMessage, PermissionResultAllow,
                              PermissionResultDeny, ResultMessage,
                              SystemMessage, TextBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage)

from conductor.claude_app import ClaudeApp
from conductor.codex_app import CodexApp
from conductor.app_web import make_backend


def _result_message(**overrides) -> ResultMessage:
    """A ResultMessage regardless of which fields this sdk version wants."""
    fields = dict(subtype="success", duration_ms=1, duration_api_ms=1,
                  is_error=False, num_turns=1, session_id="sess_1",
                  total_cost_usd=0.0, usage={}, result="")
    fields.update(overrides)
    allowed = inspect.signature(ResultMessage).parameters
    return ResultMessage(**{k: v for k, v in fields.items() if k in allowed})


class FakeClient:
    """receive_messages() yields a script; query/interrupt are recorded."""

    def __init__(self, script) -> None:
        self.script = script
        self.queries: list[str] = []
        self.interrupted = False
        self.disconnected = False

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def receive_messages(self):
        for message in self.script:
            yield message
        await asyncio.Event().wait()          # a live stream stays open


def wired(script) -> tuple[ClaudeApp, FakeClient]:
    app = ClaudeApp(cwd="/tmp")
    client = FakeClient(script)
    app._client = client
    app._events = asyncio.Queue()
    return app, client


async def drain_turn(app, text):
    events = []
    app._reader = asyncio.ensure_future(app._read())
    try:
        async for event in app.turn(text):
            events.append(event)
    finally:
        app._reader.cancel()
    return events


class ToolItems(unittest.TestCase):
    def test_bash_is_a_command_once_its_result_lands(self):
        app = ClaudeApp(cwd="/tmp")
        app._events = asyncio.Queue()
        app._commands["t1"] = "ls -la"
        app._tool_result(ToolResultBlock(tool_use_id="t1",
                                         content="total 0",
                                         is_error=False))
        event = app._events.get_nowait()
        self.assertEqual(event.kind, "item")
        self.assertEqual(event.item_type, "commandExecution")
        self.assertEqual(event.text, "ls -la")
        self.assertEqual(event.data["output"], "total 0")
        self.assertNotIn("exitCode", event.data)

    def test_a_failed_command_carries_an_exit(self):
        app = ClaudeApp(cwd="/tmp")
        app._events = asyncio.Queue()
        app._commands["t1"] = "false"
        app._tool_result(ToolResultBlock(tool_use_id="t1",
                                         content="boom", is_error=True))
        event = app._events.get_nowait()
        self.assertEqual(event.data["exitCode"], 1)

    def test_edit_is_a_file_change(self):
        event = ClaudeApp._tool_item(ToolUseBlock(
            id="t2", name="Edit", input={"file_path": "a.py"}))
        self.assertEqual(event.item_type, "fileChange")
        self.assertEqual(event.text, "a.py")
        self.assertEqual(event.data["changes"][0]["path"], "a.py")

    def test_anything_else_is_a_tool_call(self):
        event = ClaudeApp._tool_item(ToolUseBlock(
            id="t3", name="Grep", input={"pattern": "boss"}))
        self.assertEqual(event.item_type, "mcpToolCall")
        self.assertIn("Grep", event.text)


class Turns(unittest.TestCase):
    def test_a_turn_streams_text_then_settles(self):
        app, client = wired([
            SystemMessage(subtype="init",
                          data={"session_id": "sess_1", "model": "opus"}),
            AssistantMessage(content=[TextBlock(text="Looking."),
                                      ToolUseBlock(id="t", name="Bash",
                                                   input={"command": "ls"})],
                             model="opus"),
            UserMessage(content=[ToolResultBlock(tool_use_id="t",
                                                 content="a.py b.py",
                                                 is_error=False)]),
            AssistantMessage(content=[TextBlock(text="Two files.")],
                             model="opus"),
            _result_message(),
        ])
        events = asyncio.run(drain_turn(app, "what is here?"))
        self.assertEqual(client.queries, ["what is here?"])
        kinds = [e.kind for e in events]
        self.assertEqual(kinds, ["delta", "item", "delta", "turn_done"])
        self.assertEqual(app.thread_id, "sess_1")
        self.assertEqual(app.model, "opus")
        self.assertIn("Two files.", events[-1].text)

    def test_a_failed_turn_is_an_error(self):
        app, _ = wired([_result_message(subtype="error_during_execution",
                                        is_error=True)])
        events = asyncio.run(drain_turn(app, "boom"))
        self.assertEqual(events[-1].kind, "error")

    def test_interrupt_reaches_the_client(self):
        async def scenario():
            app, client = wired([])
            await app.interrupt()
            app._disarm()
            return client.interrupted
        self.assertTrue(asyncio.run(scenario()))

    def test_an_unanswered_interrupt_settles_the_turn(self):
        # Claude Code does not always answer an interrupt with a
        # ResultMessage; the watchdog cuts the turn short with what
        # was said so far.
        async def scenario():
            app, client = wired([
                AssistantMessage(content=[TextBlock(text="Half a")],
                                 model="opus"),
            ])
            app.CUT_AFTER = 0.01
            app._reader = asyncio.ensure_future(app._read())
            events = []
            try:
                async for event in app.turn("go on forever"):
                    events.append(event)
                    if event.kind == "delta":
                        await app.interrupt()
            finally:
                app._reader.cancel()
            return events
        events = asyncio.run(scenario())
        self.assertEqual([e.kind for e in events], ["delta", "error"])
        self.assertIn("Half a", events[-1].text)

    def test_a_result_disarms_the_interrupt_watchdog(self):
        async def scenario():
            app, client = wired([_result_message()])
            app.CUT_AFTER = 0.01
            await app.interrupt()
            app._reader = asyncio.ensure_future(app._read())
            events = []
            try:
                async for event in app.turn("stop"):
                    events.append(event)
            finally:
                app._reader.cancel()
            await asyncio.sleep(0.05)   # past CUT_AFTER: nothing more lands
            return events, app._events.qsize()
        events, leftover = asyncio.run(scenario())
        self.assertEqual([e.kind for e in events], ["turn_done"])
        self.assertEqual(leftover, 0)


class Approvals(unittest.TestCase):
    def _ask(self, decision):
        async def scenario():
            app, _ = wired([])
            asking = asyncio.ensure_future(
                app._can_use_tool("Bash", {"command": "rm x"}, None))
            event = await asyncio.wait_for(app._events.get(), 2)
            self.assertEqual(event.kind, "approval")
            approval = event.data["approval"]
            self.assertIn("Bash(rm x)", approval.question)
            approval.answer(decision)
            return app, await asking
        return asyncio.run(scenario())

    def test_accept_allows(self):
        _, result = self._ask("accept")
        self.assertIsInstance(result, PermissionResultAllow)

    def test_decline_denies(self):
        _, result = self._ask("decline")
        self.assertIsInstance(result, PermissionResultDeny)

    def test_accept_for_session_stops_the_asking(self):
        app, result = self._ask("acceptForSession")
        self.assertIsInstance(result, PermissionResultAllow)
        again = asyncio.run(app._can_use_tool("Bash", {"command": "ls"},
                                              None))
        self.assertIsInstance(again, PermissionResultAllow)


class Selection(unittest.TestCase):
    def test_codex_is_the_default(self):
        app, options = make_backend("/tmp")
        self.assertIsInstance(app, CodexApp)
        self.assertEqual(options.get("approval_policy"), "untrusted")

    def test_claude_is_selectable(self):
        app, options = make_backend("/tmp", "claude")
        self.assertIsInstance(app, ClaudeApp)
        self.assertEqual(options, {})


if __name__ == "__main__":
    unittest.main()
