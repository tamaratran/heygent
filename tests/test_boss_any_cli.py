"""The Boss hosted by a CLI other than Claude Code.

    Boss session (codex / gemini / cursor-agent / devin) --MCP--> boss-mcp
        --socket--> BossBridge -> conductor

What these tests hold: PtyManagerBackend names no CLI - the adapter of
the runtime that hosts the Boss writes its command line and its MCP
configuration; every adapter puts the `boss` server (boss-mcp, with the
credential) in front of the session and keeps the credential off the
command line; the session id, the record's provider and the manager
persistence follow the adapter; and a Boss another CLI left behind is
not resumed by this one.

Run with:  python3 -m unittest tests.test_boss_any_cli -v
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor.agent_events import AgentEvent
from conductor.cli_adapter import BossSpec, ClaudeCodeAdapter
from conductor.codex_adapter import CodexAdapter
from conductor.pty_manager import BOSS_TASK_ID, BossUnavailable, PtyManagerBackend
from conductor.screen_adapter import CursorAdapter, DevinAdapter, GeminiAdapter
from tests.test_boss_session import FakeBridge, FakeConductor, FakeRuntime, fake_helper

HERE = Path(__file__).resolve().parent.parent


class ProviderRuntime(FakeRuntime):
    """A provider runtime: hosts one CLI, says which through .adapter."""

    def __init__(self, adapter):
        super().__init__()
        del self.claude
        self.adapter = adapter


def spec_for(boss_dir: Path, **more) -> BossSpec:
    boss_dir.mkdir(parents=True, exist_ok=True)
    token_file = boss_dir / "mcp.token"
    token_file.write_text("tok")
    token_file.chmod(0o600)
    fields = {"boss_dir": boss_dir, "server": "boss",
              "tools": ("create_task", "send_to_task"),
              "session_id": "sid-1234", "resume": False, "boss_id": "boss_9",
              "credential": "tok", "command": "/opt/boss-mcp",
              "args": ["--socket", "/tmp/tools.sock", "--tools",
                       "create_task,send_to_task"],
              "token_file": token_file}
    fields.update(more)
    return BossSpec(**fields)


class EveryAdapterKeepsTheCredentialPrivate(unittest.TestCase):
    """The credential is the Boss's identity. It goes in a file only the
    user can read (the MCP config, or the token file boss-mcp is pointed
    at) - never on a command line `ps` shows the machine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.boss_dir = Path(self.tmp.name) / "boss"

    def tearDown(self):
        self.tmp.cleanup()

    def written(self) -> list[Path]:
        return [p for p in self.boss_dir.rglob("*") if p.is_file()]

    def check(self, adapter, argv):
        joined = " ".join(argv)
        self.assertNotIn("tok", joined.split(), f"{adapter.name}: credential on argv")
        self.assertNotIn("BOSS_MCP_TOKEN=tok", joined)
        for path in self.written():
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600",
                             f"{adapter.name}: {path.name} is not private")

    def test_claude_code_writes_mcp_json_and_names_it(self):
        adapter = ClaudeCodeAdapter("/bin/claude")
        argv = adapter.boss_argv(spec_for(self.boss_dir))
        self.check(adapter, argv)
        config = json.loads((self.boss_dir / "mcp.json").read_text())["mcpServers"]["boss"]
        self.assertEqual(config["env"]["BOSS_MCP_TOKEN"], "tok")
        self.assertEqual(argv[argv.index("--mcp-config") + 1], str(self.boss_dir / "mcp.json"))
        self.assertEqual(adapter.boss_needle(spec_for(self.boss_dir)),
                         str(self.boss_dir / "mcp.json"))

    def test_codex_takes_the_server_as_overrides_and_the_token_from_a_file(self):
        """Codex's MCP servers live in config.toml or -c overrides; an
        override is on the command line, so the credential cannot be in
        it. boss-mcp reads it from the file instead."""
        adapter = CodexAdapter("/bin/codex")
        spec = spec_for(self.boss_dir)
        argv = adapter.boss_argv(spec)
        self.check(adapter, argv)
        self.assertEqual(argv[0], "/bin/codex")
        self.assertEqual(argv[argv.index("-a") + 1], "never")
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
        self.assertIn('mcp_servers.boss.command="/opt/boss-mcp"', overrides)
        args_override = next(o for o in overrides if o.startswith("mcp_servers.boss.args="))
        args = json.loads(args_override.split("=", 1)[1])
        self.assertEqual(args[args.index("--token-file") + 1], str(spec.token_file))
        self.assertEqual(args[args.index("--boss-id") + 1], "boss_9")
        self.assertIn(f'projects."{self.boss_dir}".trust_level="trusted"', overrides)
        # Measured 0.154.0: under `-a never` every MCP call is refused
        # ("MCP tool call requires approval, but approval policy is
        # never") unless the server's tools are approved by config.
        self.assertIn('mcp_servers.boss.default_tools_approval_mode="approve"',
                      overrides)
        self.assertNotIn("resume", argv)
        self.assertNotIn("-m", argv)
        self.assertEqual(adapter.boss_needle(spec), str(spec.token_file))

    def test_codex_resumes_by_subcommand(self):
        adapter = CodexAdapter("/bin/codex")
        argv = adapter.boss_argv(spec_for(self.boss_dir, resume=True, model="gpt-5"))
        self.assertEqual(argv[1:3], ["resume", "sid-1234"])
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5")

    def test_codex_over_http_names_the_url_and_the_header(self):
        adapter = CodexAdapter("/bin/codex")
        argv = adapter.boss_argv(spec_for(
            self.boss_dir, http_entry={"type": "http", "url": "http://127.0.0.1:1/mcp",
                                       "headers": {"Authorization": "Bearer tok"}}))
        overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
        self.assertIn('mcp_servers.boss.url="http://127.0.0.1:1/mcp"', overrides)
        self.assertIn('mcp_servers.boss.http_headers.Authorization="Bearer tok"',
                      overrides)

    def test_gemini_writes_project_settings_and_pins_its_session(self):
        adapter = GeminiAdapter("/bin/gemini")
        spec = spec_for(self.boss_dir)
        argv = adapter.boss_argv(spec)
        self.check(adapter, argv)
        settings = json.loads((self.boss_dir / ".gemini" / "settings.json").read_text())
        server = settings["mcpServers"]["boss"]
        self.assertEqual(server["command"], "/opt/boss-mcp")
        self.assertEqual(server["env"]["BOSS_MCP_TOKEN"], "tok")
        self.assertIs(server["trust"], True)
        self.assertIn("run_shell_command", settings["tools"]["exclude"])
        self.assertIn("write_file", settings["tools"]["exclude"])
        # Measured 0.59.0: the workspace must be trusted BEFORE Gemini
        # reads its settings or the MCP server in them is disabled
        # ("configured but disabled because this folder is untrusted");
        # --skip-trust is applied after, so the variable goes in front.
        self.assertEqual(argv[:3], ["env", "GEMINI_CLI_TRUST_WORKSPACE=true",
                                    "/bin/gemini"])
        self.assertNotIn("--skip-trust", argv)
        self.assertEqual(argv[argv.index("--approval-mode") + 1], "yolo")
        self.assertEqual(argv[argv.index("--allowed-mcp-server-names") + 1], "boss")
        self.assertEqual(argv[argv.index("--session-id") + 1], "sid-1234")
        self.assertTrue(adapter.PINS_SESSION_ID)
        self.assertEqual(adapter.BOSS_INSTRUCTIONS, ("GEMINI.md",))

    def test_gemini_resumes_the_latest_session_of_its_directory(self):
        argv = GeminiAdapter("/bin/gemini").boss_argv(
            spec_for(self.boss_dir, resume=True))
        self.assertEqual(argv[argv.index("--resume") + 1], "latest")
        self.assertNotIn("--session-id", argv)

    def test_cursor_writes_project_mcp_json(self):
        adapter = CursorAdapter("/bin/cursor-agent")
        argv = adapter.boss_argv(spec_for(self.boss_dir))
        self.check(adapter, argv)
        config = json.loads((self.boss_dir / ".cursor" / "mcp.json").read_text())
        self.assertEqual(config["mcpServers"]["boss"]["env"]["BOSS_MCP_TOKEN"], "tok")
        self.assertEqual(argv[:3], ["/bin/cursor-agent", "--trust", "--force"])
        self.assertNotIn("--resume", argv)
        argv = adapter.boss_argv(spec_for(self.boss_dir, resume=True))
        self.assertEqual(argv[argv.index("--resume") + 1], "sid-1234")

    def test_devin_writes_project_mcp_config_and_denies_coding(self):
        adapter = DevinAdapter("/bin/devin")
        argv = adapter.boss_argv(spec_for(self.boss_dir))
        self.check(adapter, argv)
        mcp = json.loads((self.boss_dir / ".devin" / "mcp_config.json").read_text())
        self.assertEqual(mcp["mcpServers"]["boss"]["env"]["BOSS_SESSION_ID"], "boss_9")
        config = json.loads((self.boss_dir / ".devin" / "config.json").read_text())
        self.assertIn("mcp__boss__*", config["permissions"]["allow"])
        self.assertIn("Exec(*)", config["permissions"]["deny"])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dangerous")
        self.assertEqual(argv[argv.index("--respect-workspace-trust") + 1], "false")

    def test_devin_as_a_worker(self):
        adapter = DevinAdapter("/bin/devin")
        argv = adapter.launch_argv("fix the tests", "acceptEdits")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "accept-edits")
        self.assertEqual(argv[-1], "fix the tests")
        self.assertEqual(argv[argv.index("--") + 1], "fix the tests")
        self.assertEqual(adapter.resume_argv("s1")[-2:], ["--resume", "s1"])

    def test_a_cli_with_no_mcp_client_cannot_host_the_boss(self):
        from conductor.cli_adapter import CliAdapter
        self.assertIsNone(CliAdapter("/bin/cli").boss_argv(spec_for(self.boss_dir)))


class BossMcpReadsTheTokenFromAFile(unittest.TestCase):
    """--token-file, for a CLI whose MCP config is not private."""

    def test_the_file_stands_in_for_the_environment(self):
        from conductor import boss_mcp
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp.token"
            path.write_text("file-token\n")
            async def no_tools():
                return []
            with mock.patch.object(boss_mcp, "build_server") as build, \
                    mock.patch.dict(os.environ, {boss_mcp.ENV_TOKEN: "env-token"}):
                build.return_value.list_tools = no_tools
                boss_mcp.main(["--socket", "/tmp/x.sock", "--tools", "create_task",
                               "--token-file", str(path), "--boss-id", "boss_9",
                               "--list"])
            build.assert_called_once_with("/tmp/x.sock", ("create_task",),
                                          "file-token", "boss_9")

    def test_a_missing_file_is_an_error_not_an_empty_token(self):
        from conductor import boss_mcp
        with self.assertRaises(SystemExit):
            boss_mcp.main(["--socket", "/tmp/x.sock", "--token-file",
                           "/nonexistent/mcp.token", "--list"])


class TheBackendFollowsItsAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.conductor = FakeConductor()

    def tearDown(self):
        self.tmp.cleanup()

    def backend(self, runtime, **more) -> PtyManagerBackend:
        backend = PtyManagerBackend(
            runtime, self.home, self.home / "boss" / "tools.sock",
            python="/usr/bin/python3", repo_root="/repo", turn_timeout=2.0,
            helper=fake_helper(self.home), connect_timeout=0.2, **more)
        backend.attach_bridge(FakeBridge())
        return backend

    def test_the_hosting_runtimes_adapter_writes_the_command_line(self):
        runtime = ProviderRuntime(CodexAdapter("/bin/codex"))
        runtime.adapter.sessions_root = self.home / "codex-sessions"
        backend = self.backend(runtime)
        self.assertEqual(backend.adapter.name, "codex")
        self.assertIsNone(backend.model, "Claude's model alias given to Codex")
        turn = asyncio.run(backend.handle("what's going on?", self.conductor))
        (task_id, cwd, argv, _), = runtime.launches
        self.assertEqual(task_id, BOSS_TASK_ID)
        self.assertEqual(cwd, str(self.home / "boss"))
        self.assertEqual(argv[0], "/bin/codex")
        self.assertNotIn("--mcp-config", argv)
        self.assertNotIn("--session-id", argv, "Codex names its own sessions")
        token = (self.home / "boss" / "mcp.token")
        self.assertEqual(token.read_text(), "tok")
        self.assertEqual(oct(token.stat().st_mode & 0o777), "0o600")
        self.assertEqual(turn.reply, "Two tasks are open; Posely is running tests.")
        # Discovered, as a worker's is: the fake answers with its default.
        self.assertEqual(backend.session_id, "sess-boss")
        self.assertEqual(backend.session.provider, "codex")
        self.assertEqual(backend.session.provider_session_id, "sess-boss")
        self.conductor.projects.set_manager.assert_called_with("codex", "sess-boss")

    def test_the_instructions_go_where_the_cli_reads_them(self):
        runtime = ProviderRuntime(GeminiAdapter("/bin/gemini"))
        backend = self.backend(runtime)
        asyncio.run(backend.handle("hi", self.conductor))
        text = (self.home / "boss" / "GEMINI.md").read_text()
        self.assertTrue(text.startswith("# You are the Boss"))
        self.assertIn("a persistent Gemini CLI session", text)
        self.assertNotIn("Claude Code session", text)
        self.assertNotIn("mcp__boss__", text, "Claude's spelling, not Gemini's")
        self.assertIn("`mcp_boss_*`", text, "Gemini spells them mcp_<server>_<tool>")
        self.assertFalse((self.home / "boss" / "CLAUDE.md").exists())
        argv = runtime.launches[0][2]
        self.assertEqual(argv[argv.index("--session-id") + 1], backend.session_id)
        self.assertEqual(len(backend.session_id), 36, "Gemini pins a uuid")

    def test_a_cli_without_a_transcript_has_its_session_pinned_anyway(self):
        """Nothing to discover: the runtime adopts the name we gave."""
        runtime = ProviderRuntime(CursorAdapter("/bin/cursor-agent"))
        backend = self.backend(runtime)
        asyncio.run(backend.handle("hi", self.conductor))
        self.assertEqual(len(backend.session_id), 36)
        self.assertEqual(backend.session.provider, "cursor")

    def test_the_default_is_still_claude_code(self):
        backend = self.backend(FakeRuntime())
        self.assertEqual(backend.adapter.name, "claude-code")
        self.assertEqual(backend.adapter.binary, "/bin/claude")
        self.assertEqual(backend.model, "opus")
        asyncio.run(backend.handle("hi", self.conductor))
        self.assertEqual(backend.session.provider, "claude-code")
        self.conductor.projects.set_manager.assert_called_with(
            "anthropic", backend.session_id)

    def test_an_explicit_adapter_wins_over_the_runtimes(self):
        backend = self.backend(FakeRuntime(), adapter=CodexAdapter("/bin/codex"))
        self.assertEqual(backend.adapter.name, "codex")

    def test_a_signed_out_cli_fails_the_boss_at_once_and_says_how_to_sign_in(self):
        """Measured: cursor-agent and devin, signed out, sit on a login
        screen the runtime adopts; boss-mcp never connects and the Boss
        used to fail at the connect timeout with "never connected". The
        runtime's sign-in question ends the wait instead."""

        class SignedOutRuntime(ProviderRuntime):
            async def subscribe(self, session_id, handler):
                unsubscribe = await super().subscribe(session_id, handler)
                handler(AgentEvent(type="needs_input",
                                   question="Cursor needs you to sign in",
                                   detail={"reason": "auth"}))
                return unsubscribe

        class NeverHello(FakeBridge):
            async def wait_connected(self, timeout):
                await asyncio.sleep(timeout)

        runtime = SignedOutRuntime(CursorAdapter("/bin/cursor-agent"))
        backend = self.backend(runtime)
        backend.connect_timeout = 30.0
        backend.attach_bridge(NeverHello())

        async def scenario():
            started = asyncio.get_running_loop().time()
            with self.assertRaises(BossUnavailable) as caught:
                await backend.handle("hi", self.conductor)
            return str(caught.exception), \
                asyncio.get_running_loop().time() - started

        message, took = asyncio.run(scenario())
        self.assertLess(took, 5.0, "waited for the connect timeout")
        self.assertIn("Cursor is not signed in", message)
        self.assertIn("cursor-agent login", message)
        self.assertEqual(backend.session.status, "failed")
        texts = [(e.payload or {}).get("text", "")
                 for e in backend.store.events(backend.session.id)]
        self.assertTrue(any("cursor-agent login" in t for t in texts), texts)

    def test_the_login_screens_measured_signed_out_are_auth_dialogs(self):
        cursor = CursorAdapter("/bin/cursor-agent")
        self.assertEqual(cursor.startup_dialog(
            "Cursor Agent\n\n  Signing in with the browser...\n"), "auth")
        devin = DevinAdapter("/bin/devin")
        self.assertEqual(devin.startup_dialog(
            "  How would you like to log in?\n  > 1. Browser\n    2. Token\n"),
            "auth")
        for adapter in (cursor, devin, CodexAdapter("/bin/codex"),
                        GeminiAdapter("/bin/gemini"), ClaudeCodeAdapter("/bin/claude")):
            self.assertTrue(adapter.login_command, adapter.name)

    def test_a_cli_that_cannot_host_the_boss_is_refused_whole(self):
        from conductor.cli_adapter import CliAdapter
        backend = self.backend(ProviderRuntime(CliAdapter("/bin/cli")))
        with self.assertRaises(BossUnavailable):
            asyncio.run(backend.handle("hi", self.conductor))
        self.assertEqual(backend.session.status, "failed")

    def test_another_clis_session_is_not_resumed(self):
        """A record a Claude Code Boss wrote, opened by a Codex Boss: the
        timeline continues, the session is new, and the record says why."""
        first = self.backend(FakeRuntime())
        asyncio.run(first.handle("one", self.conductor))
        claude_sid = first.session_id
        self.assertEqual(first.session.provider, "claude-code")
        runtime = ProviderRuntime(CodexAdapter("/bin/codex"))
        runtime.adapter.sessions_root = self.home / "codex-sessions"
        again = self.backend(runtime)
        asyncio.run(again.handle("two", self.conductor))
        self.assertEqual(again.session.id, first.session.id, "a new conversation")
        self.assertEqual(again.session.provider, "codex")
        self.assertNotEqual(again.session_id, claude_sid)
        argv = runtime.launches[0][2]
        self.assertNotIn("resume", argv)
        notes = [e.payload.get("text", "") for e in
                 again.store.events(again.session.id) if e.type == "system_event"]
        self.assertTrue(any("was a claude-code session" in n for n in notes), notes)

    def test_a_codex_boss_resumes_its_own_rollout(self):
        root = self.home / "codex-sessions" / "2026" / "09" / "11"
        root.mkdir(parents=True)
        sid = "01a08f44-f1a3-7cb2-a3c7-a101ceaf0b3d"
        rollout = root / f"rollout-2026-09-11T07-00-55-{sid}.jsonl"
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": sid, "cwd": str(self.home / "boss")}}) + "\n")
        runtime = ProviderRuntime(CodexAdapter("/bin/codex"))
        runtime.adapter.sessions_root = self.home / "codex-sessions"
        first = self.backend(runtime)
        asyncio.run(first.handle("one", self.conductor))
        first.session.provider_session_id = sid
        first.store.save(first.session)
        runtime = ProviderRuntime(CodexAdapter("/bin/codex"))
        runtime.adapter.sessions_root = self.home / "codex-sessions"
        again = self.backend(runtime)
        asyncio.run(again.handle("two", self.conductor))
        argv = runtime.launches[0][2]
        self.assertEqual(argv[1:3], ["resume", sid])
        self.assertEqual(again.session_id, sid)

    def test_liveness_needs_no_process_check_where_nothing_names_us(self):
        """Cursor's command line names nothing of ours; the window's
        status is all there is, and a pgrep for nothing is not run."""
        backend = self.backend(ProviderRuntime(CursorAdapter("/bin/cursor-agent")))
        self.assertIsNone(backend._needle())
        with mock.patch("conductor.pty_manager.subprocess.run") as run:
            self.assertTrue(backend._process_running())
            run.assert_not_called()
        gemini = self.backend(ProviderRuntime(GeminiAdapter("/bin/gemini")))
        asyncio.run(gemini.handle("hi", self.conductor))
        self.assertEqual(gemini._needle(), "--allowed-mcp-server-names boss")


class TheLauncherPicksTheHostingRuntime(unittest.TestCase):
    def setUp(self):
        try:
            import conduct
        except ImportError:
            self.skipTest("conduct needs the audio stack")
        self.conduct = conduct

    def routing(self):
        from conductor.routing_runtime import RoutingRuntime
        local = ProviderRuntime(ClaudeCodeAdapter("/bin/claude"))
        local.claude = "/bin/claude"
        codex = ProviderRuntime(CodexAdapter("/bin/codex"))
        return RoutingRuntime(local, providers={"codex": codex}), local, codex

    def test_the_default_boss_is_the_local_claude(self):
        routing, local, _ = self.routing()
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.conduct.build_boss(routing, Path(tmp), "visible")
        self.assertIs(backend.runtime, local)
        self.assertEqual(backend.adapter.name, "claude-code")

    def test_a_named_provider_hosts_the_boss(self):
        routing, _, codex = self.routing()
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.conduct.build_boss(routing, Path(tmp), "visible",
                                              provider="codex")
        self.assertIs(backend.runtime, codex)
        self.assertEqual(backend.adapter.name, "codex")
        self.assertIsNone(backend.model)
        self.assertNotIn("hooks", backend.session_settings)

    def test_a_provider_not_installed_is_refused_plainly(self):
        routing, _, _ = self.routing()
        with tempfile.TemporaryDirectory() as tmp, \
                self.assertRaises(SystemExit) as caught:
            self.conduct.build_boss(routing, Path(tmp), "visible", provider="gemini")
        self.assertIn("gemini", str(caught.exception))
        self.assertIn("codex", str(caught.exception))

    def test_the_headless_boss_is_claude_only(self):
        routing, _, _ = self.routing()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            self.conduct.build_boss(routing, Path(tmp), "headless", provider="codex")

    def test_the_flag_exists(self):
        source = (HERE / "conduct.py").read_text()
        self.assertIn('"--boss-provider"', source)


if __name__ == "__main__":
    unittest.main()
