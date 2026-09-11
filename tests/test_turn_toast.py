"""A toast under the Boss's turn, linking the sessions it mentioned.

Claude Code shows exactly one thing under a finished turn that the model
does not see: a Stop hook's systemMessage. Measured on 2.1.251 - plain
stdout from the hook is discarded (stdout must be one JSON object),
escape sequences are stripped (so the link is a bare URL the terminal
linkifies), and the payload carries last_assistant_message, which is
what the toast is built from.

Run with:  python3 -m unittest tests.test_turn_toast -v
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from conductor import turn_toast
from conductor.jump import JumpServer

HERE = Path(__file__).resolve().parent.parent
ROWS = [
    {"task_id": "task_00eac099", "title": "Codex smoke test",
     "status": "waiting on you", "glyph": "attention",
     "url": "http://127.0.0.1:8977/s/task_00eac099"},
    {"task_id": "task_58d81737", "title": "List open PRs",
     "status": "finished - 12 open", "glyph": "done",
     "url": "http://127.0.0.1:8977/s/task_58d81737"},
    {"task_id": "task_b5b89341", "title": "Tests",
     "status": "working", "glyph": "working",
     "url": "http://127.0.0.1:8977/s/task_b5b89341"},
]


class WhatTheAnswerWasAbout(unittest.TestCase):
    def test_an_id_is_a_mention_whole_or_by_its_tail(self):
        self.assertEqual(
            [r["task_id"] for r in turn_toast.mentioned(
                "I sent it to task_58d81737 just now.", ROWS)],
            ["task_58d81737"])
        self.assertEqual(
            [r["task_id"] for r in turn_toast.mentioned(
                "00eac099 came back with the PR list.", ROWS)],
            ["task_00eac099"])

    def test_a_distinctive_title_is_a_mention(self):
        said = "I asked the Codex smoke test worker to try again."
        self.assertEqual([r["task_id"] for r in turn_toast.mentioned(said, ROWS)],
                         ["task_00eac099"])

    def test_a_short_title_is_not(self):
        """A worker called "Tests" would otherwise match every answer
        that says the word."""
        said = "I ran the tests and they pass."
        self.assertEqual(turn_toast.mentioned(said, ROWS), [])

    def test_an_answer_about_nothing_is_silent(self):
        self.assertEqual(turn_toast.mentioned("All quiet.", ROWS), [])
        self.assertEqual(turn_toast.mentioned("", ROWS), [])
        self.assertEqual(turn_toast.mentioned("task_58d81737", []), [])

    def test_they_come_in_the_order_the_answer_names_them(self):
        said = ("List open PRs finished, and then I poked "
                "task_00eac099 about the approval.")
        self.assertEqual([r["task_id"] for r in turn_toast.mentioned(said, ROWS)],
                         ["task_58d81737", "task_00eac099"])

    def test_a_toast_is_a_signpost_not_a_wall(self):
        many = [dict(ROWS[0], task_id=f"task_{i:08x}",
                     title=f"A long worker title {i}") for i in range(9)]
        said = " ".join(r["task_id"] for r in many)
        self.assertEqual(len(turn_toast.mentioned(said, many)), turn_toast.MOST)


class WhatItDraws(unittest.TestCase):
    def test_the_line_carries_state_title_status_and_the_link(self):
        line = turn_toast.toast(ROWS[:1])
        self.assertEqual(
            line, "! Codex smoke test - waiting on you · "
                  "http://127.0.0.1:8977/s/task_00eac099")

    def test_each_session_gets_its_own_line(self):
        self.assertEqual(len(turn_toast.toast(ROWS[:2]).splitlines()), 2)

    def test_no_escape_sequences_claude_code_would_strip(self):
        text = turn_toast.toast(ROWS)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("]8;;", text)


class TheHookItself(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        turn_toast.write_sessions(self.home, ROWS)

    def run_hook(self, payload) -> str:
        done = subprocess.run(
            [sys.executable, str(HERE / "conductor" / "turn_toast.py"),
             str(self.home)],
            input=payload, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_it_runs_as_a_plain_file_and_prints_one_json_object(self):
        """Claude Code discards stdout that is not one JSON object, so
        the toast would never be drawn."""
        out = self.run_hook(json.dumps(
            {"last_assistant_message": "task_58d81737 came back."}))
        message = json.loads(out)["systemMessage"]
        self.assertIn("List open PRs", message)
        self.assertIn("http://127.0.0.1:8977/s/task_58d81737", message)

    def test_it_says_nothing_when_there_is_nothing_to_say(self):
        for payload in (json.dumps({"last_assistant_message": "All quiet."}),
                        json.dumps({}), "not json at all", ""):
            self.assertEqual(self.run_hook(payload).strip(), "",
                             f"noise under every turn for {payload[:20]!r}")

    def test_it_tells_the_model_nothing(self):
        """The toast is for the person reading; feeding it back would
        have the Boss answer its own signpost."""
        out = json.loads(self.run_hook(json.dumps(
            {"last_assistant_message": "task_58d81737 came back."})))
        self.assertEqual(list(out), ["systemMessage"])

    def test_an_unwritten_sessions_file_is_silence_not_a_crash(self):
        turn_toast.sessions_path(self.home).unlink()
        self.assertEqual(self.run_hook(json.dumps(
            {"last_assistant_message": "task_58d81737"})).strip(), "")

    def test_the_file_is_replaced_whole(self):
        """The hook reads it on every turn; a half-written file would be
        a broken toast."""
        turn_toast.write_sessions(self.home, ROWS[:1])
        self.assertEqual(len(turn_toast.read_sessions(self.home)), 1)
        self.assertEqual(list(turn_toast.sessions_path(self.home).parent
                              .glob("*.tmp")), [])


class TheLinkGoesSomewhere(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def serve(self, focus):
        async def go(paths):
            server = JumpServer(self.tmp.name, focus)
            port = await server.start()
            out = []
            for path in paths:
                out.append(await asyncio.get_running_loop().run_in_executor(
                    None, self._get, f"http://127.0.0.1:{port}{path}"))
            url = server.url_for("task_abc")
            await server.stop()
            return out, port, url
        return go

    @staticmethod
    def _get(url) -> int:
        try:
            with urllib.request.urlopen(url, timeout=5) as reply:
                return reply.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_a_session_link_focuses_that_session(self):
        seen = []

        async def focus(task_id):
            seen.append(task_id)
        codes, port, url = asyncio.run(self.serve(focus)(["/s/task_abc"]))
        self.assertEqual(codes, [200])
        self.assertEqual(seen, ["task_abc"])
        self.assertEqual(url, f"http://127.0.0.1:{port}/s/task_abc")

    def test_anything_else_is_refused_without_asking_the_conductor(self):
        seen = []

        async def focus(task_id):
            seen.append(task_id)
        codes, _, _ = asyncio.run(self.serve(focus)(
            ["/s/../../etc/passwd", "/nope", "/s/"]))
        self.assertEqual(codes, [404, 404, 404])
        self.assertEqual(seen, [], "a bad path reached the conductor")

    def test_a_session_that_will_not_come_forward_says_so(self):
        async def focus(task_id):
            raise RuntimeError("its window is gone")
        codes, _, _ = asyncio.run(self.serve(focus)(["/s/task_abc"]))
        self.assertEqual(codes, [500])

    def test_the_port_is_remembered_so_an_old_toast_still_works(self):
        async def focus(task_id):
            pass

        async def twice():
            first = JumpServer(self.tmp.name, focus)
            port = await first.start()
            await first.stop()
            second = JumpServer(self.tmp.name, focus)
            again = await second.start()
            await second.stop()
            return port, again
        port, again = asyncio.run(twice())
        self.assertEqual(port, again)

    def test_a_visiting_server_leaves_the_remembered_port_alone(self):
        """app_view's demo runs beside a live conductor. Measured on
        2026-08-30: its server rewrote boss/jump.port with a port that
        died with the demo, so the conductor's old toasts opened
        nothing. persist=False is the demo staying a guest."""
        async def focus(task_id):
            pass

        async def go():
            keeper = JumpServer(self.tmp.name, focus)
            kept = await keeper.start()
            await keeper.stop()
            visitor = JumpServer(self.tmp.name, focus, persist=False)
            await visitor.start()
            await visitor.stop()
            comeback = JumpServer(self.tmp.name, focus)
            again = await comeback.start()
            await comeback.stop()
            return kept, again
        kept, again = asyncio.run(go())
        self.assertEqual(kept, again)


class TheBossIsLaunchedWithIt(unittest.TestCase):
    def test_the_window_is_fronted_by_its_child_not_the_wrapper(self):
        """The spawned pid is uv's wrapper, which owns no window; System
        Events can only front the python child that does. Measured
        2026-09-01: window_show_failed on every new chat, while fronting
        the child by hand worked."""
        try:
            import conduct
        except Exception:                      # pragma: no cover
            self.skipTest("conduct needs the audio stack")
        from unittest import mock
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            out = "29027\n" if argv[0] == "pgrep" else ""
            return subprocess.CompletedProcess(argv, 0, stdout=out,
                                               stderr="")
        with mock.patch.object(conduct.subprocess, "run",
                               side_effect=fake_run):
            conduct.raise_boss_window(29024)
        self.assertEqual(calls[0][:2], ["pgrep", "-P"])
        self.assertEqual(calls[0][2], "29024")
        self.assertIn("unix id is 29027", calls[-1][-1])

    def test_the_hook_is_this_session_s_own_setting(self):
        """--settings, so no other Claude Code session on the machine
        grows a hook and the user's own settings are untouched."""
        try:
            import conduct
        except Exception:                      # pragma: no cover
            self.skipTest("conduct needs the audio stack")
        settings = conduct.toast_hook(Path("/tmp/home"))
        entry = settings["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(entry["type"], "command")
        self.assertIn("turn_toast.py", entry["command"])
        self.assertIn("/tmp/home", entry["command"])
        self.assertLessEqual(entry["timeout"], 30)

    def test_the_interpreter_is_one_that_will_still_be_there(self):
        """Measured 2026-08-30: sys.executable under `uv run --with` is a
        temporary build env. It was deleted, and every turn the session
        took then carried "Stop hook error: ... No such file or
        directory"."""
        try:
            import conduct
        except Exception:                      # pragma: no cover
            self.skipTest("conduct needs the audio stack")
        chosen = conduct.toast_python()
        self.assertIsNotNone(chosen)
        self.assertNotIn("/.cache/uv/builds", chosen)
        self.assertTrue(Path(chosen).exists(), chosen)
        # And it can actually run the hook.
        done = subprocess.run(
            [chosen, str(HERE / "conductor" / "turn_toast.py"), "/nonexistent"],
            input="{}", capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_no_lasting_interpreter_means_no_hook_rather_than_noise(self):
        try:
            import conduct
        except Exception:                      # pragma: no cover
            self.skipTest("conduct needs the audio stack")
        from unittest import mock
        with mock.patch.object(conduct, "toast_python", return_value=None):
            self.assertEqual(conduct.toast_hook(Path("/tmp/home")), {})


if __name__ == "__main__":
    unittest.main()
