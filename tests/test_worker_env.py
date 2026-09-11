"""No worker gets the voice's OpenAI key.

The key is read from .env by the conductor, for its voice, and nothing it
starts has a use for it. It used to reach every worker anyway: the
conductor's own environment is inherited by everything it spawns, and a
tmux pane gets the tmux SERVER's global environment - the environment of
whatever started the server, which outlives the conductor that did
(measured 2026-09-10: `tmux show-environment -g` held it).

These run real processes where they can - a private tmux server, `sh` -
and ask the launched process itself. Only presence is ever reported: the
key here is a fake, and no assertion prints an environment.

Run with:  python3 -m unittest tests.test_worker_env -v
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from conductor import agent_feed
from conductor.cloud_runtime import CloudClaudeRuntime, _clean_env
from conductor.cmux_runtime import scrub_prefix
from conductor.cmux_surface import RESUME_TEMPLATE
from conductor.tmux_runtime import TmuxClaudeRuntime, scrub_argv

ROOT = Path(__file__).resolve().parent.parent
NAME = "OPENAI_API_KEY"
FAKE = "sk-fake-for-tests-only"

# What a launched worker reports about itself: one word, never a value.
PROBE = (f"import os, sys; open(sys.argv[1], 'w').write("
         f"'present' if {NAME!r} in os.environ else 'absent')")


def env_with_key() -> dict:
    env = dict(os.environ, **{NAME: FAKE})
    env.pop("TMUX", None)
    return env


def env_without_key() -> dict:
    env = dict(os.environ)
    env.pop(NAME, None)
    env.pop("TMUX", None)
    return env


def binary_index(argv: list[str]) -> int:
    """Where the `env -u NAME VAR=value ...` prefix ends."""
    i = argv.index("env") + 1
    while i < len(argv):
        if argv[i] == "-u":
            i += 2
        elif "=" in argv[i]:
            i += 1
        else:
            return i
    raise AssertionError("the launch has no binary after its env prefix")


class ProbeDir(unittest.TestCase):
    def setUp(self) -> None:
        # Short: a tmux socket path has to fit in sun_path.
        self.dir = tempfile.mkdtemp(prefix="wenv")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.seen = os.path.join(self.dir, "seen")
        self.script = os.path.join(self.dir, "probe")
        Path(self.script).write_text(
            f"#!{sys.executable}\n{PROBE.replace('sys.argv[1]', repr(self.seen))}\n")
        os.chmod(self.script, 0o755)

    def read_seen(self, timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(self.seen):
                text = Path(self.seen).read_text()
                if text:
                    return text
            time.sleep(0.05)
        self.fail("the launched process never reported")

    def run_shell(self, line: str) -> str:
        subprocess.run(["sh", "-c", line], env=env_with_key(), check=True,
                       timeout=30, capture_output=True)
        return self.read_seen()


def tmux_launch_argv() -> list[str]:
    """The argv the tmux runtime really hands `tmux new-session`."""
    runtime = TmuxClaudeRuntime(transcript_dir=None)
    seen = []

    def fake_tmux(*args):
        seen.append(args)
        return mock.Mock(returncode=1, stdout="", stderr="stop here")
    loop = asyncio.new_event_loop()
    try:
        with mock.patch.object(runtime, "_tmux", side_effect=fake_tmux):
            try:
                loop.run_until_complete(
                    runtime.create_session("task_x", "/tmp", "go"))
            except Exception:
                pass                      # the launch is what we inspect
    finally:
        loop.close()
    return list(next(a for a in seen if a and a[0] == "new-session"))


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class ATmuxServerThatAlreadyHoldsTheKey(ProbeDir):
    """The case that actually leaked: the server's global environment has
    the key, and the conductor launching the worker no longer does."""

    def setUp(self) -> None:
        super().setUp()
        self.socket = os.path.join(self.dir, "s")
        subprocess.run(["tmux", "-S", self.socket, "new-session", "-d",
                        "-s", "holder", "sleep", "120"],
                       env=env_with_key(), check=True, timeout=10)
        self.addCleanup(subprocess.run,
                        ["tmux", "-S", self.socket, "kill-server"],
                        capture_output=True, timeout=10)

    def launch(self, argv: list[str]) -> str:
        subprocess.run(["tmux", "-S", self.socket, *argv],
                       env=env_without_key(), check=True, timeout=10)
        return self.read_seen()

    def test_a_bare_pane_would_get_it(self) -> None:
        """Without this the test below could pass for the wrong reason."""
        self.assertEqual(self.launch(
            ["new-session", "-d", "-s", "bare", sys.executable, "-c", PROBE,
             self.seen]), "present")

    def test_a_launched_worker_does_not(self) -> None:
        argv = tmux_launch_argv()
        worker = argv[:binary_index(argv)] + [sys.executable, "-c", PROBE,
                                               self.seen]
        self.assertEqual(self.launch(worker), "absent")


class ShellLaunches(ProbeDir):
    """Lines typed into a shell that already has the key: a cmux
    workspace inherits cmux's environment, a tmux `sh -c` the server's."""

    def test_the_cmux_worker_line(self) -> None:
        line = scrub_prefix("cond_task_x", home="/tmp/home") + shlex.quote(
            self.script)
        self.assertEqual(self.run_shell(line), "absent")

    def test_the_cmux_surface_resume(self) -> None:
        line = RESUME_TEMPLATE.format(claude=shlex.quote(self.script),
                                      session="sess")
        self.assertEqual(self.run_shell(line), "absent")

    def test_the_cloud_launches(self) -> None:
        rt = CloudClaudeRuntime(local=mock.Mock(), claude_binary=self.script,
                                create_timeout=0.1, reveal=False)
        commands = []

        def fake_tmux(*args):
            if args[0] == "new-session":
                commands.append(args[-1])
            return mock.Mock(returncode=1, stdout="", stderr="stop here")
        loop = asyncio.new_event_loop()
        try:
            with mock.patch.object(rt, "_tmux", side_effect=fake_tmux):
                try:
                    loop.run_until_complete(
                        rt.create_session("task_x", "/tmp", "go"))
                except Exception:
                    pass
        finally:
            loop.close()
        self.assertTrue(commands, "the cloud runtime launched nothing")
        # `claude --cloud ...; sleep N` - the sleep only holds the pane.
        self.assertEqual(self.run_shell(commands[0].split(";")[0]),
                         "absent")


class CallsThatPassAnEnvironment(unittest.TestCase):
    def test_they_drop_it(self) -> None:
        with mock.patch.dict(os.environ, {NAME: FAKE}):
            for name, env in (("cloud_runtime._clean_env", _clean_env()),
                              ("agent_feed._env", agent_feed._env())):
                self.assertFalse(NAME in env, f"{name} passes {NAME} on")


class TheConductorProcess(unittest.TestCase):
    """Everything else the conductor starts - the Boss, SDK and Codex
    sessions, cmux, a new tmux server - inherits its environment, so the
    key leaves that environment as soon as the voice has it."""

    def test_after_withholding_a_child_does_not_get_it(self) -> None:
        try:
            from voice_agent import withhold_api_key
        except Exception:          # the audio stack is not installed
            self.skipTest("voice_agent needs the audio stack")
        with mock.patch.dict(os.environ, {NAME: FAKE}):
            withhold_api_key()
            done = subprocess.run(
                [sys.executable, "-c",
                 f"import os; print({NAME!r} in os.environ)"],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(done.stdout.strip(), "False")

    def test_both_entry_points_withhold_it_before_starting_anything(self):
        for name in ("conduct.py", "voice_agent.py"):
            source = (ROOT / name).read_text()
            body = source[source.index("async def main("):]
            read = body.index(f'os.environ.get("{NAME}"')
            withheld = body.find("withhold_api_key()", read)
            self.assertNotEqual(withheld, -1,
                                f"{name} never withholds the key")
            first_spawn = body.index("create_subprocess_exec")
            self.assertLess(withheld, first_spawn,
                            f"{name} starts a process that still has it")
            if "build_conductor(" in body:
                self.assertLess(withheld, body.index("build_conductor("))


if __name__ == "__main__":
    unittest.main()
