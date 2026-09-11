"""The Boss as a Codex CLI session.

What these tests hold, each measured on codex-cli 0.151.0 first
(conductor/codex_boss.py has the measurements):

  - boss-mcp's credential is in a 0700 launcher, never on codex's
    command line, and its tools are approved (Codex refuses an MCP call
    under approval policy "never" otherwise);
  - the user's own MCP servers are switched off by their bare names, and
    only feature flags this Codex knows are passed (an unknown one stops
    the launch);
  - a fresh Codex Boss is adopted at its prompt under a handle, and its
    real id is read off the rollout its first message creates - read from
    the first line, so that message is seen - and saved for a restart;
  - a stored id resumes only when its rollout is a Codex session in the
    Boss's directory, so a Claude Code id starts a new session instead;
  - the window gets a Codex message whole, paragraph breaks and all.

Run with:  python3 -m unittest tests.test_codex_boss -v
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import codex_boss
from conductor.agent_events import AgentEvent
from conductor.codex_adapter import CodexAdapter
from conductor.pty_manager import BOSS_TASK_ID, BossUnavailable, PtyManagerBackend
from conductor.tmux_runtime import PENDING_PREFIX, TmuxClaudeRuntime, _TmuxSession
from tests.test_boss_session import (FakeBridge, FakeConductor, FakeRuntime,
                                     fake_helper)

SID = "01a08f42-5960-7642-bdf5-0afb52198df5"


def rollout(root: Path, cwd: str, sid: str = SID, lines=()) -> Path:
    path = root / "2026" / "09" / "11" / f"rollout-2026-09-11T00-00-00-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [{"type": "session_meta", "payload": {"id": sid, "cwd": cwd}},
               *lines]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def user(text):
    return {"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": text}]}}


class TheCommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_credential_lives_in_a_launcher_only_the_user_can_read(self):
        launcher = codex_boss.write_launcher(
            self.dir, Path("/opt/boss-mcp"), Path("/tmp/tools.sock"),
            ("create_task", "send_to_task"), "boss_1", "s3cret'token")
        self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o700)
        text = launcher.read_text()
        self.assertIn("exec /opt/boss-mcp --socket /tmp/tools.sock "
                      "--tools create_task,send_to_task", text)
        argv = codex_boss.argv("/bin/codex", launcher, features=set(),
                               others=[])
        self.assertNotIn("s3cret", " ".join(argv))
        self.assertIn(f'mcp_servers.boss.command="{launcher}"', argv)

    def test_its_tools_are_approved_and_nothing_else_writes(self):
        argv = codex_boss.argv("/bin/codex", self.dir / "l", features=set(),
                               others=[])
        self.assertIn('mcp_servers.boss.default_tools_approval_mode="approve"', argv)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertEqual(argv[argv.index("-a") + 1], "never")
        self.assertIn("notify=[]", argv)
        self.assertIn("check_for_update_on_startup=false", argv)

    def test_other_servers_are_switched_off_by_their_bare_names(self):
        argv = codex_boss.argv("/bin/codex", self.dir / "l", features=set(),
                               others=["computer-use", "node_repl", "a.b"])
        self.assertIn("mcp_servers.computer-use.enabled=false", argv)
        self.assertIn("mcp_servers.node_repl.enabled=false", argv)
        # A quoted key made a new server named with its quotes; a name
        # bare TOML cannot spell is not touched at all.
        self.assertFalse(any("a.b" in part or '"' + "computer" in part
                             for part in argv))

    def test_only_features_this_codex_knows_are_disabled(self):
        argv = codex_boss.argv("/bin/codex", self.dir / "l",
                               features={"shell_tool", "plugins", "unrelated"},
                               others=[])
        disabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]
        self.assertEqual(sorted(disabled), ["plugins", "shell_tool"])
        none = codex_boss.argv("/bin/codex", self.dir / "l", features=None)
        self.assertNotIn("--disable", none)

    def test_a_resume_names_the_id_last(self):
        argv = codex_boss.argv("/bin/codex", self.dir / "l", resume=SID,
                               features=set(), others=[])
        self.assertEqual(argv[:2], ["/bin/codex", "resume"])
        self.assertEqual(argv[-1], SID)

    def test_other_servers_come_from_codex_own_listing(self):
        listing = json.dumps([{"name": "boss", "enabled": True},
                              {"name": "node_repl", "enabled": True},
                              {"name": "computer-use", "enabled": False}])
        done = mock.Mock(stdout=listing, returncode=0)
        with mock.patch.object(codex_boss.subprocess, "run",
                               return_value=done) as run:
            self.assertEqual(codex_boss.other_servers("/bin/codex", ["plugins"]),
                             ["node_repl"])
        self.assertEqual(run.call_args.args[0],
                         ["/bin/codex", "--disable", "plugins", "mcp", "list", "--json"])


class WhatResumes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sessions"
        self.boss_dir = Path(self.tmp.name) / "boss"
        self.boss_dir.mkdir()
        self.adapter = CodexAdapter()
        self.adapter.sessions_root = self.root

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_codex_session_from_this_directory(self):
        rollout(self.root, str(self.boss_dir))
        self.assertTrue(codex_boss.resumable(self.adapter, self.boss_dir, SID))

    def test_not_one_from_elsewhere_nor_a_claude_code_id(self):
        rollout(self.root, "/somewhere/else")
        self.assertFalse(codex_boss.resumable(self.adapter, self.boss_dir, SID))
        self.assertFalse(codex_boss.resumable(
            self.adapter, self.boss_dir, "5f1c2d3e-0000-4000-8000-000000000000"))


class Screen:
    """has-session, capture-pane: a Codex at its prompt."""

    def __call__(self, *args):
        return mock.Mock(returncode=0, stdout="› Ask Codex to do anything\n",
                         stderr="")


class TheIdComesWithTheFirstMessage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sessions"
        self.cwd = str(Path(self.tmp.name) / "boss")
        os.makedirs(self.cwd)
        adapter = CodexAdapter()
        adapter.sessions_root = self.root
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        rt.adapter = adapter
        rt.sessions = {}
        rt.startup_timeout = 1.0
        rt._tmux = Screen()
        rt.DISCOVER_EVERY_S = 0
        rt.bus = mock.Mock()
        self.rt = rt

    def tearDown(self):
        self.tmp.cleanup()

    def test_adopted_at_its_prompt_then_read_from_the_first_line(self):
        rt = self.rt
        old = rollout(self.root, self.cwd, sid="00000000-0000-4000-8000-000000000000")

        async def scenario():
            rt._claim_name = mock.AsyncMock(return_value="free")
            rt._wait_resumed = mock.AsyncMock()
            with mock.patch("conductor.tmux_runtime.stream_path",
                            return_value=Path(self.tmp.name) / "s" / "boss.raw"):
                handle = await rt.launch_session(BOSS_TASK_ID, self.cwd,
                                                 ["codex"], focus=False,
                                                 adopt_at_prompt=True)
            sess = rt.sessions[handle]
            sess.watcher.cancel()
            seen = []
            sess.handlers.append(seen.append)
            before = rt.provider_session_id(handle)
            self.assertFalse(await rt._watch_once(sess))   # nothing yet
            rollout(self.root, self.cwd, lines=[user("what projects do I have?")])
            await rt._watch_once(sess)
            return handle, before, rt.provider_session_id(handle), seen, sess
        handle, before, after, seen, sess = asyncio.run(scenario())
        self.assertTrue(handle.startswith(PENDING_PREFIX))
        self.assertIsNone(before)
        self.assertEqual(after, SID)
        self.assertNotEqual(sess.jsonl_path, old)
        self.assertIn("> what projects do I have?", [e.summary for e in seen])

    def test_a_known_id_is_its_own_provider_id(self):
        self.rt.sessions["abc"] = _TmuxSession(task_id="t", name="n",
                                               working_directory=self.cwd,
                                               session_id="abc")
        self.assertEqual(self.rt.provider_session_id("abc"), "abc")
        self.assertIsNone(self.rt.provider_session_id("nobody"))


class LateRuntime(FakeRuntime):
    """A runtime that knows the Codex id only once a message is in."""

    def __init__(self):
        super().__init__()
        self.adapter = CodexAdapter("/bin/codex")
        self.known = None
        self.kwargs = {}

    async def launch_session(self, task_id, working_directory, argv,
                             existing=None, session_id=None, focus=True,
                             adopt_at_prompt=False):
        self.kwargs = {"session_id": session_id, "adopt_at_prompt": adopt_at_prompt}
        self.launches.append((task_id, working_directory, argv, existing))
        sid = session_id or "pending_1"
        self.statuses[sid] = "idle"
        return sid

    def provider_session_id(self, handle):
        return self.known if handle.startswith("pending_") else handle

    async def send(self, session_id, message):
        self.sent.append((session_id, message))
        self.known = SID
        for handler in list(self.handlers.get(session_id, [])):
            handler(AgentEvent(type="progress", summary=f"> {message}",
                               detail={"source": "user_message"}))
            handler(AgentEvent(type="progress", summary="Looking.",
                               text="Looking.\n\nStill looking."))
            handler(AgentEvent(type="completed", summary="One project: Owlery."))


class TheCodexBoss(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.runtime = LateRuntime()
        self.conductor = FakeConductor()
        self.conductor.projects.manager.return_value = {"session_id": "claude-old"}
        self.backend = PtyManagerBackend(
            self.runtime, self.home, self.home / "boss" / "tools.sock",
            python="/usr/bin/python3", repo_root="/repo", turn_timeout=2.0,
            helper=fake_helper(self.home), connect_timeout=0.2, cli="codex")
        self.backend.SETTLE_S = 0.05
        self.backend.attach_bridge(FakeBridge())
        self.argv = mock.patch.object(codex_boss, "launch_argv",
                                      side_effect=lambda b, l, r: codex_boss.argv(
                                          b, l, resume=r, features=set(), others=[]))
        self.argv.start()

    def tearDown(self):
        self.argv.stop()
        self.tmp.cleanup()

    def test_a_turn_records_the_id_codex_gave_it(self):
        prose = []
        self.backend.on_prose = prose.append
        turn = asyncio.run(self.backend.handle("what projects?", self.conductor))
        self.assertEqual(turn.reply, "One project: Owlery.")
        self.assertEqual(self.runtime.kwargs, {"session_id": None,
                                               "adopt_at_prompt": True})
        self.assertEqual(self.backend.session.provider_session_id, SID)
        saved = json.loads((self.home / "boss" / "sessions" / self.backend.session.id
                            / "session.json").read_text())
        self.assertEqual(saved["provider_session_id"], SID)
        # A Codex id is not the Claude manager's to resume.
        self.conductor.projects.set_manager.assert_not_called()
        self.assertEqual(prose, ["Looking.\n\nStill looking."])

    def test_it_never_inherits_the_claude_manager_id(self):
        asyncio.run(self.backend.handle("hi", self.conductor))
        argv = self.runtime.launches[0][2]
        self.assertNotIn("claude-old", argv)
        self.assertNotIn("resume", argv)

    def test_its_instructions_and_launcher_are_codex_shaped(self):
        asyncio.run(self.backend.handle("hi", self.conductor))
        boss_dir = self.home / "boss"
        self.assertTrue((boss_dir / "AGENTS.md").exists())
        self.assertFalse((boss_dir / "mcp.json").exists())
        argv = self.runtime.launches[0][2]
        self.assertEqual(argv[0], "/bin/codex")
        self.assertIn(f'mcp_servers.boss.command="{boss_dir / codex_boss.LAUNCHER}"',
                      argv)
        self.assertNotIn("--model", argv)

    def test_a_stored_codex_id_resumes_and_a_claude_one_does_not(self):
        asyncio.run(self.backend.handle("hi", self.conductor))
        record = self.backend.session
        self.backend.rebind()
        with mock.patch.object(codex_boss, "resumable", return_value=True):
            asyncio.run(self.backend.handle("again", self.conductor))
        self.assertEqual(self.runtime.kwargs, {"session_id": SID,
                                               "adopt_at_prompt": False})
        self.assertEqual(self.runtime.launches[-1][2][-1], SID)
        self.backend.rebind()
        record.provider_session_id = "claude-uuid"
        self.backend.session = None
        self.backend.store.save(record)
        with mock.patch.object(codex_boss, "resumable", return_value=False):
            asyncio.run(self.backend.handle("third", self.conductor))
        self.assertEqual(self.runtime.kwargs["adopt_at_prompt"], True)

    def test_http_transport_is_refused_with_the_way_out(self):
        self.backend.transport = "http"
        with self.assertRaises(BossUnavailable) as caught:
            asyncio.run(self.backend.handle("hi", self.conductor))
        self.assertIn("--boss-transport stdio", str(caught.exception))

    def test_its_process_is_found_by_the_launcher_it_names(self):
        with mock.patch("conductor.pty_manager.subprocess.run") as run:
            run.return_value = mock.Mock(stdout="123\n")
            self.assertTrue(self.backend._process_running())
        self.assertIn(str(self.home / "boss" / codex_boss.LAUNCHER),
                      run.call_args.args[0])


class CodexReadForTheWindow(unittest.TestCase):
    def test_an_assistant_message_is_given_whole(self):
        entry = {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text",
                         "text": "Owls hunt at night.\n\nThey fly silently."}]}}
        event, = CodexAdapter().normalize(entry, {})
        self.assertEqual(event.text, "Owls hunt at night.\n\nThey fly silently.")
        self.assertEqual(event.summary, "Owls hunt at night. They fly silently.")

    def test_the_update_dialog_is_skipped_not_taken(self):
        screen = ("  ✨ Update available! 0.151.0 -> 0.154.0\n"
                  "› 1. Update now (runs `npm install -g @openai/codex`)\n"
                  "  2. Skip\n  3. Skip until next version\n"
                  "  Press enter to continue\n")
        adapter = CodexAdapter()
        self.assertEqual(adapter.startup_dialog(screen), "update")
        self.assertFalse(adapter.prompt_ready(screen))
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        rt.adapter = adapter
        keys = []
        rt._tmux = lambda *args: keys.append(args[3:]) or mock.Mock(returncode=0)
        rt._handle_startup_prompts(_TmuxSession(task_id="t", name="n",
                                                working_directory="/tmp"), screen)
        self.assertEqual(keys, [("Down",), ("Enter",)])


class TheLauncherPicksTheHost(unittest.TestCase):
    def test_no_codex_on_path_says_so(self):
        try:
            import conduct
        except Exception as exc:          # the voice stack is not installed
            self.skipTest(f"conduct.py does not import here: {exc}")
        routing = mock.Mock(runtimes={"local": FakeRuntime()})
        with self.assertRaises(SystemExit) as caught:
            conduct.build_boss(routing, Path("/tmp/h"), "visible", "stdio", "codex")
        self.assertIn("--boss-cli claude-code", str(caught.exception))
        codex = FakeRuntime()
        routing.runtimes["codex"] = codex
        backend = conduct.build_boss(routing, Path("/tmp/h"), "visible", "stdio",
                                     "codex")
        self.assertIs(backend.runtime, codex)
        self.assertEqual(backend.cli, "codex")
        self.assertEqual(backend.session_settings, {})


if __name__ == "__main__":
    unittest.main()
