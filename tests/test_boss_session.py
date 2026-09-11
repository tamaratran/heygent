"""The Boss as a session you can see.

    Boss session (cmux) --stdio--> boss-mcp --socket--> BossBridge -> conductor

What these tests hold: the bridge serves only the credential it was bound
to and records every call on the turn; the MCP server offers exactly the
conductor's tools with the conductor's schemas; the backend refuses to be
ready until boss-mcp has said hello with the required tools, resumes the
session id the invisible Boss already had, types the same context the
invisible Boss was handed, and reads the reply from the transcript the
way a worker's is read; and the Boss never gets a coding tool.

Run with:  python3 -m unittest tests.test_boss_session -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor.agent_events import AgentEvent
from conductor.boss_bridge import BossBridge, call_over_socket, hello_over_socket
from conductor.boss_tools import PROTOCOL_VERSION, REQUIRED_TOOLS, SCHEMAS
from conductor.manager import ToolCall
from conductor.pty_manager import (BOSS_TASK_ID, DISALLOWED, BossUnavailable,
                                   PtyManagerBackend)

TOKEN, BOSS = "secret-token", "boss_test"
AUTH = {"token": TOKEN, "boss_id": BOSS}
# The fake runtime answers instantly; the real settle wait is for a real
# session that may say more after a turn end. Kept tiny here.
PtyManagerBackend.SETTLE_S = 0.05


class FakeConductor:
    def __init__(self):
        self.bus = mock.Mock()
        self.actions = []
        self.projects = mock.Mock()
        self.projects.manager.return_value = {"session_id": None}
        self.MANAGER_TOOLS = tuple(REQUIRED_TOOLS) + ("list_tasks",)
        self.boss_store = None
        self.parents = []

    async def handle_action(self, tool, args):
        self.actions.append((tool, args))
        if tool == "boom":
            raise RuntimeError("no such tool")
        return {"echo": args}

    def set_parent_boss(self, task_id, boss_id):
        self.parents.append((task_id, boss_id))

    def global_context(self):
        return "Known projects: none"


class TheBridgeServesItsBoss(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = Path(self.tmp.name) / "tools.sock"
        self.conductor = FakeConductor()

    def tearDown(self):
        self.tmp.cleanup()

    def run_bridge(self, coro):
        async def go():
            bridge = BossBridge(self.sock, self.conductor, json.dumps)
            bridge.expect(BOSS, TOKEN)
            await bridge.start()
            try:
                return await coro(bridge)
            finally:
                await bridge.stop()
        return asyncio.run(go())

    def test_a_call_with_the_credential_reaches_the_conductor(self):
        async def scenario(bridge):
            reply = await call_over_socket(self.sock, "send_to_task",
                                           {"task_id": "t1", "message": "go"},
                                           token=TOKEN, boss_id=BOSS)
            return reply, bridge.drain()
        reply, calls = self.run_bridge(scenario)
        self.assertTrue(reply["ok"])
        self.assertEqual(self.conductor.actions,
                         [("send_to_task", {"task_id": "t1", "message": "go"})])
        self.assertEqual([c.tool for c in calls], ["send_to_task"])

    def test_the_wrong_credential_is_refused_and_runs_nothing(self):
        """Identity is the process's, not the model's: a caller that is
        not this Boss gets nothing, however plausible its arguments."""
        async def scenario(bridge):
            wrong = await call_over_socket(self.sock, "cancel_task",
                                           {"task_id": "t1"},
                                           token="guess", boss_id=BOSS)
            other = await call_over_socket(self.sock, "cancel_task",
                                           {"task_id": "t1"},
                                           token=TOKEN, boss_id="boss_other")
            return wrong, other
        wrong, other = self.run_bridge(scenario)
        self.assertFalse(wrong["ok"])
        self.assertFalse(other["ok"])
        self.assertEqual(self.conductor.actions, [])

    def test_hello_binds_and_reports_the_tools(self):
        async def scenario(bridge):
            reply = await hello_over_socket(self.sock, TOKEN, BOSS,
                                            PROTOCOL_VERSION, ["find_project"])
            return reply, await bridge.wait_connected(1.0)
        reply, connected = self.run_bridge(scenario)
        self.assertTrue(reply["ok"])
        self.assertEqual(connected["tools"], ["find_project"])

    def test_a_helper_speaking_another_protocol_is_refused(self):
        async def scenario(bridge):
            reply = await hello_over_socket(self.sock, TOKEN, BOSS,
                                            PROTOCOL_VERSION + 1, ["find_project"])
            return reply, await bridge.wait_connected(0.1)
        reply, connected = self.run_bridge(scenario)
        self.assertFalse(reply["ok"])
        self.assertIn("incompatible", reply["error"])
        self.assertIsNone(connected, "an incompatible helper counted as connected")

    def test_a_failing_tool_is_an_error_not_a_crash(self):
        async def scenario(bridge):
            return (await call_over_socket(self.sock, "boom", {}, token=TOKEN,
                                           boss_id=BOSS), bridge.drain())
        reply, calls = self.run_bridge(scenario)
        self.assertFalse(reply["ok"])
        self.assertIn("no such tool", reply["error"])
        self.assertTrue(calls[0].result.startswith("error:"))

    def test_every_call_is_on_the_bus_with_its_execution_id(self):
        async def scenario(bridge):
            await call_over_socket(self.sock, "list_tasks", {}, token=TOKEN,
                                   boss_id=BOSS)
        self.run_bridge(scenario)
        events = [c.args[0] for c in self.conductor.bus.emit.call_args_list]
        started = next(e for e in events if e.type == "boss.tool_call")
        finished = next(e for e in events if e.type == "boss.tool_result")
        self.assertEqual(started.data["execution_id"], finished.data["execution_id"])
        self.assertEqual(started.data["boss_session_id"], BOSS)


class TheMcpServerOffersTheBossTools(unittest.TestCase):
    def test_tools_and_schemas_come_from_one_source(self):
        from conductor.boss_mcp import build_server
        server = build_server("/nonexistent.sock",
                              ("send_to_task", "create_task", "situation"))
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
        self.assertEqual(set(tools), {"send_to_task", "create_task", "situation"})
        self.assertEqual(set(tools["send_to_task"].input_schema["required"]),
                         set(SCHEMAS["send_to_task"]))
        self.assertEqual(set(tools["create_task"].input_schema["properties"]),
                         set(SCHEMAS["create_task"]))

    def test_a_refusal_reaches_the_model_as_words_not_an_exception(self):
        """The MCP layer reduces a raised exception to "Error executing
        tool <name>"; the reason - "needs Screen Recording - grant..." -
        is the part the Boss must relay, so it comes back as content."""
        from conductor.boss_mcp import _handler
        reason = ("cannot start a computer-use worker yet: needs Screen "
                  "Recording - grant it in System Settings, then ask again")
        with mock.patch("conductor.boss_bridge.call_over_socket",
                        new=mock.AsyncMock(
                            return_value={"ok": False, "error": reason})):
            handler = _handler("create_task", SCHEMAS["create_task"],
                               "/nonexistent.sock", TOKEN, BOSS)
            answer = asyncio.run(handler(project="p", title="t", goal="g",
                                         background=False, computer=True))
        self.assertEqual(answer, f"error: {reason}")

    def test_the_namespace_is_boss(self):
        from conductor.boss_mcp import build_server
        self.assertEqual(build_server("/x.sock", ("situation",)).name, "boss")

    def test_version_and_list_need_no_conductor(self):
        from conductor.boss_mcp import main
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--version"]), 0)
            self.assertEqual(main(["--socket", "/nonexistent.sock", "--list",
                                   "--tools", "list_tasks,pause_task"]), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], f"boss-mcp protocol {PROTOCOL_VERSION}")
        names = sorted(json.loads(l)["name"] for l in lines[1:])
        self.assertEqual(names, ["list_tasks", "pause_task"])


class FakeBridge:
    """What the backend needs from a bridge, with a hello the test can
    withhold or send with the wrong tools."""

    def __init__(self, tools=None, hello=True):
        self.tools = list(tools if tools is not None else REQUIRED_TOOLS)
        self.hello = hello
        self.expected = None
        self.on_start = self.on_finish = None
        self.calls = []

    @staticmethod
    def new_token():
        return "tok"

    def expect(self, boss_id, token):
        self.expected = (boss_id, token)

    async def wait_connected(self, timeout):
        return {"protocol": PROTOCOL_VERSION, "tools": self.tools} if self.hello else None

    def drain(self):
        calls, self.calls = self.calls, []
        return calls


class FakeRuntime:
    """Records the launch and the sends; the test plays the transcript."""

    def __init__(self):
        self.claude = "/bin/claude"
        self.launches = []
        self.sent = []
        self.handlers = {}
        self.statuses = {}

    async def launch_session(self, task_id, working_directory, argv,
                             existing=None, session_id=None, focus=True):
        self.launches.append((task_id, working_directory, argv, existing))
        self.focus = focus
        # A known id is adopted as given; discovery is the fallback.
        sid = session_id or "sess-boss"
        self.statuses[sid] = "idle"
        return sid

    def pinned(self) -> str:
        """The id the last launch was told to use."""
        argv = self.launches[-1][2]
        for flag in ("--session-id", "--resume"):
            if flag in argv:
                return argv[argv.index(flag) + 1]
        return ""

    async def subscribe(self, session_id, handler):
        self.handlers.setdefault(session_id, []).append(handler)
        return lambda: self.handlers[session_id].remove(handler)

    async def get_status(self, session_id):
        # A string, as the real runtimes answer. The fake used to hand
        # back an object with .status, and the backend's liveness check
        # read that attribute - so the tests passed while every live
        # turn relaunched the Boss.
        return self.statuses.get(session_id, "disconnected")

    async def send(self, session_id, message):
        self.sent.append((session_id, message))
        for handler in list(self.handlers.get(session_id, [])):
            handler(AgentEvent(type="progress", summary="mcp__boss__list_tasks()",
                               detail={"tool": "mcp__boss__list_tasks"}))
            handler(AgentEvent(type="completed",
                               summary="Two tasks are open; Posely is running tests."))


import contextlib


@contextlib.contextmanager
def resumable_transcript(boss_dir: Path, session_id: str):
    """A transcript for session_id where Claude Code would look for it
    when running in boss_dir - the condition for a resume."""
    from conductor import tmux_runtime
    with tempfile.TemporaryDirectory() as projects:
        folder = Path(projects) / tmux_runtime.munge_project_dir(str(boss_dir))
        folder.mkdir(parents=True)
        (folder / f"{session_id}.jsonl").write_text("{}\n")
        with mock.patch.object(tmux_runtime, "CLAUDE_PROJECTS", Path(projects)):
            yield


def fake_helper(directory: Path) -> Path:
    """An executable that answers --version the way boss-mcp does."""
    path = directory / "boss-mcp"
    path.write_text("#!/bin/sh\n"
                    f'[ "$1" = "--version" ] && echo "boss-mcp protocol {PROTOCOL_VERSION}"\n')
    path.chmod(0o755)
    return path


class TheBossRunsInAWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.runtime = FakeRuntime()
        self.conductor = FakeConductor()
        self.bridge = FakeBridge()
        self.backend = self.make_backend()

    def make_backend(self, bridge=None, helper=None):
        backend = PtyManagerBackend(
            self.runtime, self.home, self.home / "boss" / "tools.sock",
            python="/usr/bin/python3", repo_root="/repo", turn_timeout=2.0,
            helper=helper or fake_helper(self.home), connect_timeout=0.2)
        backend.attach_bridge(bridge or self.bridge)
        return backend

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_is_hosted_like_a_worker_with_its_own_tools(self):
        turn = asyncio.run(self.backend.handle("what's going on?", self.conductor))
        (task_id, cwd, argv, _), = self.runtime.launches
        self.assertEqual(task_id, BOSS_TASK_ID)
        self.assertEqual(cwd, str(self.home / "boss"))
        self.assertIn("--mcp-config", argv)
        self.assertIn("--strict-mcp-config", argv)
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertTrue(all(a.startswith("mcp__boss__") for a in allowed))
        self.assertIn("mcp__boss__create_task", allowed)
        self.assertEqual(turn.reply, "Two tasks are open; Posely is running tests.")

    def test_it_starts_on_opus_low_effort_fast_and_in_focus_view(self):
        # Asked 2026-09-10. Session-only: a flag and --settings, never the
        # user's settings files; the toast hook's settings still ride along.
        self.backend.session_settings["hooks"] = {"Stop": []}
        asyncio.run(self.backend.handle("hi", self.conductor))
        argv = self.runtime.launches[0][2]
        self.assertEqual(argv[argv.index("--effort") + 1], "low")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertIs(settings["fastMode"], True)
        self.assertEqual(settings["viewMode"], "focus")
        self.assertEqual(settings["tui"], "fullscreen")
        self.assertEqual(settings["hooks"], {"Stop": []})

    def test_the_config_names_the_verified_helper_and_carries_the_credential(self):
        asyncio.run(self.backend.handle("hi", self.conductor))
        config = json.loads((self.home / "boss" / "mcp.json").read_text())
        server = config["mcpServers"]["boss"]
        self.assertEqual(server["command"], str(self.home / "boss-mcp"))
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["env"]["BOSS_MCP_TOKEN"], "tok")
        self.assertEqual(server["env"]["BOSS_SESSION_ID"], self.backend.session.id)
        self.assertEqual(self.bridge.expected, (self.backend.session.id, "tok"))

    def test_the_boss_never_gets_a_coding_tool(self):
        asyncio.run(self.backend.handle("hi", self.conductor))
        argv = self.runtime.launches[0][2]
        disallowed = argv[argv.index("--disallowedTools") + 1].split(",")
        for tool in ("Bash", "Edit", "Write", "Task"):
            self.assertIn(tool, disallowed)
        self.assertEqual(tuple(disallowed), DISALLOWED)
        # A rule naming a tool Claude Code no longer has ("MultiEdit",
        # folded into "Edit") makes it warn at every launch that the
        # deny rule matches no known tool.
        self.assertNotIn("MultiEdit", disallowed)

    def test_its_instructions_explain_how_it_is_running(self):
        asyncio.run(self.backend.handle("hi", self.conductor))
        text = (self.home / "boss" / "CLAUDE.md").read_text()
        self.assertTrue(text.startswith("# You are the Boss"))
        self.assertIn("mcp__boss__", text)
        self.assertIn("Never invent a task, session or approval id", text)

    def test_it_resumes_the_session_the_invisible_boss_had(self):
        """When that session's transcript is where this Boss runs."""
        self.conductor.projects.manager.return_value = {"session_id": "209bb486-old"}
        with resumable_transcript(self.home / "boss", "209bb486-old"):
            asyncio.run(self.backend.handle("hi", self.conductor))
        argv = self.runtime.launches[0][2]
        self.assertEqual(argv[argv.index("--resume") + 1], "209bb486-old")
        self.assertNotIn("--session-id", argv)

    def test_a_session_from_another_directory_is_not_resumed_blindly(self):
        """Claude Code scopes resume to the working directory. The
        invisible Boss ran in the repo; its transcript is not here. Resuming
        it would launch a session that never reaches its prompt."""
        self.conductor.projects.manager.return_value = {"session_id": "209bb486-old"}
        asyncio.run(self.backend.handle("hi", self.conductor))
        argv = self.runtime.launches[0][2]
        self.assertNotIn("--resume", argv)
        self.assertIn("--session-id", argv)
        notes = [e.payload.get("text", "") for e in
                 self.backend.store.events(self.backend.session.id)
                 if e.type == "system_event"]
        self.assertTrue(any("not resumable" in n for n in notes),
                        "started fresh without saying why")

    def test_a_new_conversation_starts_a_new_session_not_the_old_one(self):
        """`--new-chat` printed a fresh Boss id and then resumed the old
        800 KB conversation into it: every new record inherited the
        invisible Boss's session id, not only the first. The Boss woke up
        with all its old context and, asked "what's up", re-created a
        task the user had just cancelled. The inheritance is a migration,
        and a migration happens once."""
        self.conductor.projects.manager.return_value = {"session_id": "209bb486-old"}
        with resumable_transcript(self.home / "boss", "209bb486-old"):
            asyncio.run(self.backend.handle("one", self.conductor))
        first = self.backend.session.id
        self.assertEqual(self.runtime.pinned(), "209bb486-old")   # once
        # "New voice chat": a conversation asked for anew, in a new process.
        self.backend.store.new_conversation()
        runtime = FakeRuntime()
        again = PtyManagerBackend(runtime, self.home, self.home / "boss" / "tools.sock",
                                  python="/x", repo_root="/repo",
                                  helper=fake_helper(self.home), connect_timeout=0.2)
        again.attach_bridge(FakeBridge())
        with resumable_transcript(self.home / "boss", "209bb486-old"):
            asyncio.run(again.handle("two", self.conductor))
        self.assertNotEqual(again.session.id, first)
        argv = runtime.launches[0][2]
        self.assertNotIn("--resume", argv, "the new chat resumed the old one")
        self.assertIn("--session-id", argv)
        self.assertNotEqual(runtime.pinned(), "209bb486-old")
        self.assertEqual(len(again.store.list()), 2)

    def test_a_fresh_boss_pins_its_own_session_id(self):
        """Known before launch, so the runtime adopts the session at its
        prompt instead of waiting for a transcript that a session nobody
        has spoken to yet never writes."""
        asyncio.run(self.backend.handle("hi", self.conductor))
        argv = self.runtime.launches[0][2]
        self.assertIn("--session-id", argv)
        pinned = self.runtime.pinned()
        self.assertEqual(len(pinned), 36)
        self.conductor.projects.set_manager.assert_called_with("anthropic", pinned)
        self.assertEqual(self.backend.session_id, pinned)

    def test_the_window_shows_the_users_words_and_nothing_else(self):
        """The invisible Boss got clock, capabilities, registry and
        "User says:" in its user message. In a window that is the
        transcript the user reads: their words, unwrapped."""
        asyncio.run(self.backend.handle("start the\nlogin fix", self.conductor))
        (_, message), = self.runtime.sent
        self.assertEqual(message, "start the login fix")
        instructions = (self.backend.boss_dir / "CLAUDE.md").read_text()
        self.assertIn("What the user says arrives as their words", instructions)

    def test_the_window_shows_the_users_words_not_the_voice_sides_block(self):
        """Measured: the window showed "[breath] Alright. Uh. How do I quit
        this whole thing ... [what the user actually said, verbatim, most
        recent last] ... Act on these words. The line above this block
        ..." - the voice side's backstop for a Boss on a wire, read back by
        the user as if they had said it."""
        from conductor.manager import VERBATIM_FOOTER, VERBATIM_HEADER
        text = ("Alright. How do I quit this and restart it\n\n"
                f"{VERBATIM_HEADER}\n"
                "Alright . Uh. How do I quit this whole thing and restart it\n"
                f"{VERBATIM_FOOTER} The line above this block is the voice "
                "frontend's summary and may be a bare confirmation.")
        asyncio.run(self.backend.handle(text, self.conductor))
        (_, message), = self.runtime.sent
        self.assertEqual(message,
                         "Alright . Uh. How do I quit this whole thing and restart it")
        said = [e for e in self.backend.store.events(self.backend.session.id)
                if e.type == "user_message"][-1]
        self.assertEqual(said.payload["text"], message)
        self.assertEqual(said.payload["frontend_summary"],
                         "Alright. How do I quit this and restart it")

    def test_the_boss_is_opened_ahead_of_the_first_words(self):
        """Measured: ~30 s to open, all of it paid by the first utterance
        after a restart. Warmed at startup, the first turn finds it."""
        self.assertTrue(asyncio.run(self.backend.warm(self.conductor)))
        self.assertEqual(len(self.runtime.launches), 1)
        asyncio.run(self.backend.handle("hi", self.conductor))
        self.assertEqual(len(self.runtime.launches), 1, "relaunched")

    def test_a_failed_warm_up_is_logged_and_the_first_turn_tries_again(self):
        bridge, self.backend._bridge = self.backend._bridge, None
        self.assertFalse(asyncio.run(self.backend.warm(self.conductor)))
        self.assertEqual(self.runtime.launches, [])
        self.backend._bridge = bridge
        asyncio.run(self.backend.handle("hi", self.conductor))
        self.assertEqual(len(self.runtime.launches), 1)

    def test_the_first_sentence_of_a_slow_turn_is_spoken_meanwhile(self):
        """Measured: "I'll start two workers" came 4.6 s into a turn that
        ran 30 s more; the user heard nothing until the end."""
        heard = []
        self.backend.on_interim = heard.append
        self.backend.INTERIM_AFTER_S = 0.02

        async def slow_send(session_id, message):
            self.runtime.sent.append((session_id, message))
            for handler in list(self.runtime.handlers.get(session_id, [])):
                handler(AgentEvent(type="progress", summary=f"> {message}",
                                   detail={"source": "user_message"}))
                handler(AgentEvent(type="progress",
                                   summary="Starting two workers for you."))
            await asyncio.sleep(0.08)
            for handler in list(self.runtime.handlers.get(session_id, [])):
                handler(AgentEvent(type="completed",
                                   summary="Starting two workers for you. "
                                           "Both are up."))
        self.runtime.send = slow_send
        turn = asyncio.run(self.backend.handle("start two workers", self.conductor))
        self.assertEqual(heard, ["Starting two workers for you."])
        self.assertIn("Both are up", turn.reply)

    def test_a_quick_turn_is_not_spoken_twice(self):
        heard = []
        self.backend.on_interim = heard.append
        self.backend.INTERIM_AFTER_S = 0.05

        async def quick_send(session_id, message):
            self.runtime.sent.append((session_id, message))
            for handler in list(self.runtime.handlers.get(session_id, [])):
                handler(AgentEvent(type="progress", summary=f"> {message}",
                                   detail={"source": "user_message"}))
                handler(AgentEvent(type="progress", summary="Two tasks are open."))
                handler(AgentEvent(type="completed", summary="Two tasks are open."))
        self.runtime.send = quick_send
        asyncio.run(self.backend.handle("what's open", self.conductor))
        self.assertEqual(heard, [])

    # -- the interim and the answer, in either order ---------------------------
    def _played_by_hand(self):
        """The fake types nothing back; the test plays the transcript.
        Returns (play, typed): play delivers one event to the session,
        typed(n) waits until n utterances have gone in."""
        async def record_only(session_id, message):
            self.runtime.sent.append((session_id, message))
        self.runtime.send = record_only

        def play(**event):
            for handlers in list(self.runtime.handlers.values()):
                for handler in list(handlers):
                    handler(AgentEvent(**event))

        async def typed(n):
            for _ in range(400):
                if len(self.runtime.sent) >= n:
                    return
                await asyncio.sleep(0.005)
            self.fail(f"utterance {n} was never typed")
        return play, typed

    def test_a_sentence_arriving_after_its_answer_is_not_spoken_again(self):
        """Measured 2026-08-30 23:32:11-13Z: "Hey - what do you want done?"
        was returned to the voice at 11.742; its prose line reached the
        watcher one tick later; a second utterance was queued (open, not
        yet read), so the interim's "is a turn still open?" guard passed
        and the same words were spoken again at 13.242 - "Hey, what do
        you want done? Hey, what do you want done?"."""
        heard = []
        self.backend.on_interim = heard.append
        self.backend.INTERIM_AFTER_S = 0.02
        self.backend.SETTLE_S = 0.01
        answer = "Hey — what do you want done?"
        play, typed = self._played_by_hand()

        async def scenario():
            first = asyncio.create_task(
                self.backend.handle("Hey, hello", self.conductor))
            await typed(1)
            play(type="progress", summary="> Hey, hello",
                 detail={"source": "user_message"})
            second = asyncio.create_task(
                self.backend.handle("Could you explain PR 109", self.conductor))
            await typed(2)                          # queued behind the first
            play(type="completed", summary=answer)  # the first is answered
            reply = (await first).reply             # and handed to the voice
            play(type="progress", summary=answer,   # its prose line, late
                 detail={"source": ""})
            await asyncio.sleep(self.backend.INTERIM_AFTER_S * 4)
            play(type="progress", summary="> Could you explain PR 109",
                 detail={"source": "user_message"})
            play(type="completed", summary="Sure.")
            return reply, (await second).reply
        reply, second = asyncio.run(scenario())
        self.assertEqual(reply, answer)
        self.assertEqual(second, "Sure.")
        self.assertEqual(heard, [], "the answer was spoken again as an interim")

    def test_the_answer_arriving_just_after_the_sentence_claims_the_interim(self):
        """The sentence, then the whole answer 4 ms later - before the
        interim timer. The answer is spoken by the voice; the timer must
        not speak the sentence as well."""
        heard = []
        self.backend.on_interim = heard.append
        self.backend.INTERIM_AFTER_S = 0.05
        self.backend.SETTLE_S = 0.01
        play, typed = self._played_by_hand()

        async def scenario():
            turn = asyncio.create_task(
                self.backend.handle("what's open", self.conductor))
            await typed(1)
            play(type="progress", summary="> what's open",
                 detail={"source": "user_message"})
            play(type="progress", summary="Two tasks are open.",
                 detail={"source": ""})
            await asyncio.sleep(0.004)
            play(type="completed", summary="Two tasks are open.")
            reply = (await turn).reply
            await asyncio.sleep(self.backend.INTERIM_AFTER_S * 2)
            return reply
        self.assertEqual(asyncio.run(scenario()), "Two tasks are open.")
        self.assertEqual(heard, [])

    def test_a_slow_turn_is_still_narrated_with_words_queued_behind_it(self):
        """The fix above must not silence the interim it was built for:
        the session has read the first utterance and gone off to run
        tools; a second one is queued. The first sentence is spoken."""
        heard = []
        self.backend.on_interim = heard.append
        self.backend.INTERIM_AFTER_S = 0.02
        self.backend.SETTLE_S = 0.01
        play, typed = self._played_by_hand()

        async def scenario():
            first = asyncio.create_task(
                self.backend.handle("start two workers", self.conductor))
            await typed(1)
            play(type="progress", summary="> start two workers",
                 detail={"source": "user_message"})
            second = asyncio.create_task(
                self.backend.handle("and what's open", self.conductor))
            await typed(2)
            play(type="progress", summary="Starting two workers for you.",
                 detail={"source": ""})
            await asyncio.sleep(self.backend.INTERIM_AFTER_S * 4)
            play(type="completed",
                 summary="Starting two workers for you. Both are up.")
            reply = (await first).reply
            play(type="progress", summary="> and what's open",
                 detail={"source": "user_message"})
            play(type="completed", summary="Two.")
            await second
            return reply
        self.assertIn("Both are up", asyncio.run(scenario()))
        self.assertEqual(heard, ["Starting two workers for you."])

    def test_words_the_session_let_go_are_typed_again(self):
        """Measured 2026-08-31 09:13:35: a barge-in sat queued in Claude
        Code's input box, was dropped when the running turn ended, and
        the user's interruption was never answered. One loss earns a
        retype, not a shrug."""
        self.backend.SETTLE_S = 0.01
        play, typed = self._played_by_hand()

        async def scenario():
            first = asyncio.create_task(
                self.backend.handle("start two workers", self.conductor))
            await typed(1)
            play(type="progress", summary="> start two workers",
                 detail={"source": "user_message"})
            barge = asyncio.create_task(
                self.backend.handle("stop, how many tasks", self.conductor))
            await typed(2)
            # The session never wrote the barge-in; the next thing on the
            # transcript is a THIRD utterance being read.
            third = asyncio.create_task(
                self.backend.handle("thanks", self.conductor))
            await typed(3)
            play(type="completed", summary="Both are up.")
            await first
            play(type="progress", summary="> thanks",
                 detail={"source": "user_message"})
            # The loss was noticed and the barge-in typed a second time.
            await typed(4)
            play(type="progress", summary="> stop, how many tasks",
                 detail={"source": "user_message"})
            play(type="completed", summary="Three tasks.")
            answer = await barge
            play(type="completed", summary="You're welcome.")
            await third
            return answer
        answer = asyncio.run(scenario())
        self.assertEqual(answer.reply, "Three tasks.")
        self.assertEqual(self.runtime.sent[3][1], "stop, how many tasks")

    def test_words_lost_twice_are_let_go(self):
        """One retype per utterance: a session that drops the same words
        twice gets no third copy, and the turn ends unanswered rather
        than looping."""
        self.backend.SETTLE_S = 0.01
        play, typed = self._played_by_hand()

        async def scenario():
            first = asyncio.create_task(
                self.backend.handle("one", self.conductor))
            await typed(1)
            play(type="progress", summary="> one",
                 detail={"source": "user_message"})
            barge = asyncio.create_task(
                self.backend.handle("two", self.conductor))
            await typed(2)
            third = asyncio.create_task(
                self.backend.handle("three", self.conductor))
            await typed(3)
            play(type="completed", summary="Done with one.")
            await first
            play(type="progress", summary="> three",
                 detail={"source": "user_message"})
            await typed(4)                      # the retype went in
            fourth = asyncio.create_task(
                self.backend.handle("four", self.conductor))
            await typed(5)
            play(type="completed", summary="Done with three.")
            await third
            # The retyped copy is skipped again: a later line is read.
            play(type="progress", summary="> four",
                 detail={"source": "user_message"})
            answer = await barge                # resolved as lost, no reply
            play(type="completed", summary="Done with four.")
            await fourth
            return answer, len(self.runtime.sent)
        answer, sends = asyncio.run(scenario())
        self.assertEqual(answer.reply, "")
        self.assertTrue(answer.folded)
        self.assertEqual(sends, 5, "a second retype went in")

    def test_one_launch_serves_many_turns(self):
        asyncio.run(self.backend.handle("one", self.conductor))
        asyncio.run(self.backend.handle("two", self.conductor))
        self.assertEqual(len(self.runtime.launches), 1)
        self.assertEqual(len(self.runtime.sent), 2)

    def test_a_turn_that_never_ends_is_reported_not_hung(self):
        async def silent(session_id, message):
            self.runtime.sent.append((session_id, message))
        self.runtime.send = silent
        self.backend.turn_timeout = 0.05
        turn = asyncio.run(self.backend.handle("hi", self.conductor))
        self.assertIn("still in progress", turn.reply)

    def test_closing_leaves_the_session_running(self):
        asyncio.run(self.backend.handle("hi", self.conductor))
        asyncio.run(self.backend.close())
        self.assertEqual(self.runtime.statuses[self.runtime.pinned()], "idle")


class ABossWithoutItsToolsIsNotStarted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.runtime = FakeRuntime()
        self.conductor = FakeConductor()

    def tearDown(self):
        self.tmp.cleanup()

    def backend(self, bridge, helper=None):
        b = PtyManagerBackend(self.runtime, self.home, self.home / "boss" / "tools.sock",
                              python="/x", repo_root="/repo", turn_timeout=1.0,
                              helper=helper or fake_helper(self.home),
                              connect_timeout=0.1)
        b.attach_bridge(bridge)
        return b

    def test_no_hello_means_not_ready_and_says_so(self):
        b = self.backend(FakeBridge(hello=False))
        with self.assertRaises(BossUnavailable) as caught:
            asyncio.run(b.handle("hi", self.conductor))
        self.assertIn("Boss orchestration tooling is unavailable", str(caught.exception))
        self.assertEqual(b.store.get(b.session.id).status, "failed")

    def test_a_missing_required_tool_means_not_ready(self):
        b = self.backend(FakeBridge(tools=["find_project", "list_tasks"]))
        with self.assertRaises(BossUnavailable) as caught:
            asyncio.run(b.handle("hi", self.conductor))
        self.assertIn("create_task", str(caught.exception))

    def test_a_missing_helper_means_not_launched(self):
        b = self.backend(FakeBridge(), helper=self.home / "does-not-exist")
        with self.assertRaises(BossUnavailable):
            asyncio.run(b.handle("hi", self.conductor))
        self.assertEqual(self.runtime.launches, [], "launched a Boss with no tools")

    def test_a_helper_on_the_wrong_protocol_means_not_launched(self):
        wrong = self.home / "old-boss-mcp"
        wrong.write_text('#!/bin/sh\necho "boss-mcp protocol 0"\n')
        wrong.chmod(0o755)
        b = self.backend(FakeBridge(), helper=wrong)
        with self.assertRaises(BossUnavailable) as caught:
            asyncio.run(b.handle("hi", self.conductor))
        self.assertIn("incompatible", str(caught.exception))
        self.assertEqual(self.runtime.launches, [])


class TheBossKeepsARecord(unittest.TestCase):
    """The spec's acceptance walks 48-58, against the backend with a fake
    runtime, a real bridge over a real socket, and a real store on disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.runtime = FakeRuntime()
        self.conductor = FakeConductor()
        self.bridge = BossBridge(self.home / "boss" / "tools.sock",
                                 self.conductor, json.dumps)
        # The real bridge, with the hello a real boss-mcp would send
        # arriving as soon as the session is "launched".
        real_launch = self.runtime.launch_session

        async def launch_and_hello(*args, **kwargs):
            sid = await real_launch(*args, **kwargs)
            self.bridge._hello({"protocol": PROTOCOL_VERSION,
                                "tools": list(REQUIRED_TOOLS)})
            return sid
        self.runtime.launch_session = launch_and_hello
        self.backend = PtyManagerBackend(
            self.runtime, self.home, self.bridge.socket_path,
            python="/x", repo_root="/repo", turn_timeout=2.0,
            helper=fake_helper(self.home), connect_timeout=1.0)
        self.backend.attach_bridge(self.bridge)

    def tearDown(self):
        self.tmp.cleanup()

    def auth(self):
        return {"token": self.backend._credential, "boss_id": self.backend.session.id}

    def events(self):
        return self.backend.store.events(self.backend.session.id)

    def types(self):
        return [e.type for e in self.events()]

    def test_the_first_turn_creates_one_boss_bound_to_the_conversation(self):
        self.assertIsNone(self.backend.store.current_boss())
        asyncio.run(self.backend.handle("Hello.", self.conductor))
        boss = self.backend.store.current_boss()
        self.assertEqual(boss.provider_session_id, self.runtime.pinned())
        self.assertEqual(boss.status, "ready")
        self.assertEqual(len(self.backend.store.list()), 1)
        self.assertIn("user_message", self.types())
        self.assertIn("boss_message", self.types())

    def test_ten_turns_are_one_boss_and_one_execution(self):
        for i in range(10):
            asyncio.run(self.backend.handle(f"turn {i}", self.conductor))
        self.assertEqual(len(self.backend.store.list()), 1)
        self.assertEqual(len(self.runtime.launches), 1)
        self.assertEqual(self.types().count("user_message"), 10)
        sequences = [e.sequence for e in self.events()]
        self.assertEqual(sequences, sorted(set(sequences)))

    def test_tool_calls_are_durable_with_their_results(self):
        asyncio.run(self.backend.handle("check pushes", self.conductor))

        async def via_bridge():
            await self.bridge.start()
            try:
                await call_over_socket(self.bridge.socket_path, "list_tasks", {},
                                       **self.auth())
                await call_over_socket(self.bridge.socket_path, "boom", {},
                                       **self.auth())
            finally:
                await self.bridge.stop()
        asyncio.run(via_bridge())
        started = [e for e in self.events() if e.type == "tool_started"]
        finished = [e for e in self.events()
                    if e.type in ("tool_completed", "tool_failed")]
        self.assertEqual({e.payload["execution_id"] for e in started},
                         {e.payload["execution_id"] for e in finished})
        failed = next(e for e in finished if e.type == "tool_failed")
        self.assertIn("no such tool", failed.payload["error"])

    def test_starting_a_worker_links_the_child_both_ways(self):
        asyncio.run(self.backend.handle("fix login in posely", self.conductor))

        async def create():
            self.conductor.handle_action = self._create_task
            await self.bridge.start()
            try:
                await call_over_socket(self.bridge.socket_path, "create_task",
                                       {"project_id": "p", "title": "Posely · Fix login",
                                        "goal": "g"}, **self.auth())
            finally:
                await self.bridge.stop()
        asyncio.run(create())
        created = next(e for e in self.events() if e.type == "subagent_created")
        self.assertEqual(created.payload["subagent_id"], "sub_task_new")
        boss = self.backend.store.get(self.backend.session.id)
        self.assertEqual(boss.child_subagent_ids, ["sub_task_new"])
        self.assertEqual(self.conductor.parents, [("task_new", boss.id)])

    async def _create_task(self, tool, args):
        return {"task_id": "task_new", "title": args["title"], "status": "running"}

    def test_a_worker_event_received_is_on_the_record(self):
        from conductor.supervisor_inbox import SupervisoryEvent
        asyncio.run(self.backend.handle("hi", self.conductor))
        self.backend.record_supervisory(SupervisoryEvent(
            event_id="sup_1", task_id="task_a", subagent_id="sub_task_a",
            type="completed", summary="12 tests passed", requires_action=False,
            task_title="Posely · Fix login", trace_id="trc_1"))
        received = next(e for e in self.events()
                        if e.type == "subagent_event_received")
        self.assertEqual(received.payload["summary"], "12 tests passed")
        self.assertEqual(received.trace_id, "trc_1")

    def test_a_typed_turn_lands_in_the_same_timeline(self):
        asyncio.run(self.backend.handle("check posely", self.conductor))
        self.backend._on_event(AgentEvent(type="progress",
                                          summary="> actually check billing too",
                                          detail={"source": "user_message"}))
        self.backend._on_event(AgentEvent(type="completed",
                                          summary="Checking billing as well."))
        events = self.events()
        typed = next(e for e in events if e.payload.get("source") == "typed"
                     and e.type == "user_message")
        self.assertEqual(typed.payload["text"], "actually check billing too")
        self.assertEqual(events[-1].type, "boss_message")
        self.assertEqual(len(self.backend.store.list()), 1)

    def test_a_restart_resumes_the_same_boss_and_history(self):
        asyncio.run(self.backend.handle("one", self.conductor))
        asyncio.run(self.backend.handle("two", self.conductor))
        boss_id = self.backend.session.id
        runtime = FakeRuntime()
        bridge = FakeBridge()
        again = PtyManagerBackend(runtime, self.home, self.home / "boss" / "tools.sock",
                                  python="/x", repo_root="/repo",
                                  helper=fake_helper(self.home), connect_timeout=0.2)
        again.attach_bridge(bridge)
        stored = self.backend.store.get(boss_id).provider_session_id
        with resumable_transcript(self.home / "boss", stored):
            asyncio.run(again.handle("three", self.conductor))
        self.assertEqual(again.session.id, boss_id, "a second Boss was created")
        argv = runtime.launches[0][2]
        self.assertEqual(argv[argv.index("--resume") + 1], stored)
        texts = [e.payload.get("text") for e in again.store.events(boss_id)
                 if e.type == "user_message"]
        self.assertEqual(texts, ["one", "two", "three"])
        self.assertEqual(len(again.store.list()), 1)

    def test_a_dead_helper_does_not_make_a_second_boss(self):
        """boss-mcp is disposable. Its bridge connection dropping and
        coming back changes nothing about who the Boss is."""
        asyncio.run(self.backend.handle("one", self.conductor))
        boss_id = self.backend.session.id

        async def reconnect():
            await self.bridge.start()
            await self.bridge.stop()          # the helper died...
            await self.bridge.start()         # ...Claude Code restarted it
            try:
                reply = await hello_over_socket(self.bridge.socket_path,
                                                self.backend._credential, boss_id,
                                                PROTOCOL_VERSION, list(REQUIRED_TOOLS))
            finally:
                await self.bridge.stop()
            return reply
        self.assertTrue(asyncio.run(reconnect())["ok"])
        asyncio.run(self.backend.handle("two", self.conductor))
        self.assertEqual(self.backend.session.id, boss_id)
        self.assertEqual(len(self.runtime.launches), 1)


try:                       # the audio stack is not installed everywhere
    import conduct
except Exception:          # pragma: no cover
    conduct = None


@unittest.skipIf(conduct is None, "conduct needs the audio stack")
class TheProductBuildsAVisibleBoss(unittest.TestCase):
    def test_visible_is_the_default_and_headless_is_the_old_backend(self):
        from conductor.claude_manager import ClaudeManagerBackend
        with tempfile.TemporaryDirectory() as tmp:
            visible = conduct.build_boss(mock.Mock(runtimes={}), Path(tmp), "visible")
            self.assertIsInstance(visible, PtyManagerBackend)
            headless = conduct.build_boss(mock.Mock(runtimes={}), Path(tmp), "headless")
            self.assertIsInstance(headless, ClaudeManagerBackend)

    def test_the_boss_is_hosted_by_the_local_runtime_not_the_router(self):
        local, cloud = mock.Mock(), mock.Mock()
        router = mock.Mock(runtimes={"local": local, "cloud": cloud})
        with tempfile.TemporaryDirectory() as tmp:
            built = conduct.build_boss(router, Path(tmp), "visible")
        self.assertIs(built.runtime, local)

    def test_the_conductor_knows_its_boss_store(self):
        source = Path(conduct.__file__).read_text()
        self.assertIn("built.boss_store = built.manager.store", source)
        self.assertIn("record(supervisory)", source)
        self.assertIn("await bridge.start()", source)


if __name__ == "__main__":
    unittest.main()
