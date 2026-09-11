"""The Boss's window comes forward when the user speaks to it - once.

Measured before: cmux jumped in front the moment the conductor started,
before any turn at all - and quitting cmux brought it straight back,
because the Boss lives inside it and was relaunched in front again. Now
the host is launched hidden, the Boss's window is opened without focus,
and it is put in front of the user once, on the first voice turn
(SHOW_AFTER_TURNS). A worker starting still raises its own window: that
is the one moment the user is certainly looking for it.

Run with:  python3 -m unittest tests.test_boss_window_stays_back -v
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_boss_session import (FakeBridge, FakeConductor, FakeRuntime,   # noqa: E402
                               fake_helper)
from test_cmux_runtime import Base                                         # noqa: E402
from test_cmux_self_repair import CLOSED                                   # noqa: E402

from conductor import cmux_setup                                           # noqa: E402
from conductor.cmux_runtime import CmuxClaudeRuntime                       # noqa: E402
from conductor.pty_manager import PtyManagerBackend                        # noqa: E402
from conductor.tmux_runtime import TmuxClaudeRuntime                       # noqa: E402


class ShowingRuntime(FakeRuntime):
    """A runtime that can put a session in front, and remembers when."""

    def __init__(self):
        super().__init__()
        self.shown = []

    def bring_forward(self, session_id):
        self.shown.append(session_id)


class TheBossComesForwardOnTheFirstTurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.runtime = ShowingRuntime()
        self.conductor = FakeConductor()
        self.backend = PtyManagerBackend(
            self.runtime, self.home, self.home / "boss" / "tools.sock",
            python="/usr/bin/python3", repo_root="/repo", turn_timeout=2.0,
            helper=fake_helper(self.home), connect_timeout=0.2)
        self.backend.attach_bridge(FakeBridge())
        self.backend.SETTLE_S = 0.0

    def tearDown(self):
        self.tmp.cleanup()

    def say(self, text):
        return asyncio.run(self.backend.handle(text, self.conductor))

    def events(self):
        return [c.args[0].type for c in self.conductor.bus.emit.call_args_list]

    def test_the_boss_is_opened_without_focus(self):
        self.say("hey")
        self.assertIs(self.runtime.focus, False)

    def test_the_first_turn_puts_it_in_front_once(self):
        self.say("hey")
        boss = self.backend.session_id
        self.assertEqual(self.runtime.shown, [boss])
        self.say("and the other one?")
        self.say("ok")
        self.assertEqual(self.runtime.shown, [boss], "once")
        self.assertEqual(self.events().count("boss.window_shown"), 1)

    def test_the_turn_that_shows_it_is_the_configured_one(self):
        self.backend.SHOW_AFTER_TURNS = 3
        self.say("hey")
        self.say("how's it going")
        self.assertEqual(self.runtime.shown, [])
        self.assertNotIn("boss.window_shown", self.events())
        self.say("what are the workers up to")
        self.assertEqual(self.runtime.shown, [self.backend.session_id])

    def test_a_window_of_our_own_beats_the_runtime(self):
        """With the voice-agent window open the Boss has no cmux workspace;
        show_window raises that window instead, and the runtime is not
        asked."""
        raised = []
        self.backend.show_window = lambda: raised.append(True)
        self.say("hey")
        self.say("again")
        self.assertEqual(raised, [True])
        self.assertEqual(self.runtime.shown, [])
        self.assertEqual(self.events().count("boss.window_shown"), 1)

    def test_a_runtime_with_no_window_to_raise_is_fine(self):
        self.backend.runtime = self.runtime = FakeRuntime()
        for text in ("a", "b", "c", "d"):
            self.say(text)
        self.assertNotIn("boss.window_shown", self.events())

    def test_a_failure_to_show_is_logged_not_raised(self):
        self.runtime.bring_forward = mock.Mock(side_effect=RuntimeError("no cmux"))
        for text in ("a", "b"):
            self.say(text)
        turn = self.say("c")
        self.assertTrue(turn.reply)
        self.assertNotIn("boss.window_shown", self.events())


class TheCmuxRuntimeKnowsAQuietLaunch(Base):
    def create(self, rt, name="cond_task_a"):
        return rt._create(["new-session", "-d", "-s", name, "-c", "/tmp"])

    def test_a_worker_is_raised_as_it_starts(self):
        rt = self.runtime()
        self.assertEqual(self.create(rt).returncode, 0)
        self.assertEqual(self.raised, [True])

    def test_a_quiet_launch_is_not(self):
        rt = self.runtime()
        rt._quiet().add("cond_task_a")
        self.assertEqual(self.create(rt).returncode, 0)
        self.assertEqual(self.raised, [])
        self.assertNotIn(("select-workspace", "--workspace", "WS-UUID"), self.calls)

    def test_launch_session_marks_the_name_only_while_launching(self):
        rt = self.runtime()
        seen = {}

        async def launching(self_, task_id, *a, **kw):
            seen["quiet"] = set(rt._quiet())
            return "sid"
        with mock.patch.object(TmuxClaudeRuntime, "launch_session", launching):
            sid = asyncio.run(rt.launch_session("boss", "/tmp", ["claude"], focus=False))
        self.assertEqual(sid, "sid")
        self.assertEqual(seen["quiet"], {"cond_boss"})
        self.assertEqual(rt._quiet(), set(), "cleared even on the way out")

    def test_launch_session_defaults_to_a_window_in_front(self):
        rt = self.runtime()
        seen = {}

        async def launching(self_, task_id, *a, **kw):
            seen["quiet"] = set(rt._quiet())
            return "sid"
        with mock.patch.object(TmuxClaudeRuntime, "launch_session", launching):
            asyncio.run(rt.launch_session("task_a", "/tmp", ["claude"]))
        self.assertEqual(seen["quiet"], set())

    def test_bring_forward_finds_the_session_by_its_name(self):
        rt = self.runtime()
        rt.sessions = {"sid": SimpleNamespace(name="cond_task_a")}
        rt.bring_forward("sid")
        self.assertEqual(self.raised, [True])
        self.assertIn(("select-workspace", "--workspace", "WS-UUID"), self.calls)
        self.assertIn(("focus-window", "--window", "WIN-UUID"), self.calls)

    def test_bring_forward_of_nothing_does_nothing(self):
        rt = self.runtime()
        rt.sessions = {}
        rt.bring_forward("nope")
        self.assertEqual(self.raised, [])

    def test_a_tmux_pane_has_no_window_to_raise(self):
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        self.assertIsNone(rt.bring_forward("anything"))


class CmuxIsLaunchedHidden(unittest.TestCase):
    def test_the_launch_is_in_the_background(self):
        self.assertEqual(cmux_setup.LAUNCH, ("open", "-g", "-j", "-a", "cmux"))

    def test_ensure_running_launches_it_that_way(self):
        ran = []

        async def run(*args, timeout=0):
            ran.append(args)
            return 0, ""
        with mock.patch.object(cmux_setup, "_run", run), \
                mock.patch.object(asyncio, "create_subprocess_exec",
                                  side_effect=OSError("no socket")):
            up = asyncio.run(cmux_setup.ensure_running("/fake/cmux", "pw", attempts=0))
        self.assertFalse(up)
        self.assertEqual(ran, [cmux_setup.LAUNCH])

    def repair(self, running):
        """repair() with cmux in the given running states; what it ran."""
        ran = []
        with mock.patch.object(cmux_setup, "is_running", side_effect=running), \
                mock.patch.object(cmux_setup.subprocess, "run",
                                  side_effect=lambda cmd, **kw: ran.append(tuple(cmd))), \
                mock.patch.object(cmux_setup, "hide_app",
                                  side_effect=lambda **kw: ran.append("hide")), \
                mock.patch.object(cmux_setup.time, "sleep"), \
                mock.patch.object(cmux_setup, "ensure_socket_access", return_value="pw"), \
                mock.patch.object(cmux_setup, "_ping_sync", return_value=True):
            self.assertEqual(cmux_setup.repair("/fake/cmux"), "pw")
        return ran

    def test_repair_launches_it_that_way_and_then_hides_it(self):
        """-j alone does not keep it hidden (measured: visible, behind
        the front app, once it had a workspace), so it is hidden as it
        comes up and again once it answers."""
        self.assertEqual(self.repair([False, True]),
                         [cmux_setup.LAUNCH, "hide", "hide"])

    def test_hiding_keeps_at_it_while_the_window_keeps_coming_back(self):
        """One hide is undone by cmux showing its window as it finishes
        launching (measured: hidden at 0.6 s, visible from 0.5 s on)."""
        seen = iter(["false", "true", "true", "false", "false"])
        ran = []

        def run(cmd, **kw):
            ran.append(cmd[-1][-5:])       # "false" (set) or "cmux\"" (get)
            if "get visible" in cmd[-1]:
                return mock.Mock(stdout=next(seen, "false"))
            return mock.Mock(stdout="")
        with mock.patch.object(cmux_setup.subprocess, "run", run), \
                mock.patch.object(cmux_setup.time, "sleep"), \
                mock.patch.object(cmux_setup.time, "monotonic",
                                  side_effect=[0, 0, 1, 2, 3, 4, 5, 6, 7]):
            cmux_setup.hide_app(for_s=4.0)
        self.assertEqual(ran.count("false"), 2, "hidden each time it showed")

    def test_repair_leaves_a_cmux_it_did_not_launch_alone(self):
        """The user's own cmux, open where they put it, is not hidden
        because the password needed rewriting."""
        self.assertEqual(self.repair([True]), [])

    def test_ensure_running_hides_what_it_launched(self):
        ran, hidden = [], []

        async def run(*args, timeout=0):
            ran.append(args)
            return 0, ""
        pings = iter([False, True])

        async def exec_(*args, **kw):
            if not next(pings):
                raise OSError("no socket")
            proc = mock.Mock(returncode=0)
            proc.communicate = mock.AsyncMock(return_value=(b"PONG", b""))
            return proc
        with mock.patch.object(cmux_setup, "_run", run), \
                mock.patch.object(cmux_setup, "hide_app", lambda **kw: hidden.append(True)), \
                mock.patch.object(asyncio, "create_subprocess_exec", exec_), \
                mock.patch.object(asyncio, "sleep", mock.AsyncMock()):
            up = asyncio.run(cmux_setup.ensure_running("/fake/cmux", "pw", attempts=1))
        self.assertTrue(up)
        self.assertEqual(ran, [cmux_setup.LAUNCH])
        self.assertEqual(hidden, [True])

    def test_hiding_is_best_effort(self):
        with mock.patch.object(cmux_setup.subprocess, "run", side_effect=OSError("no osascript")), \
                mock.patch.object(cmux_setup.time, "sleep"):
            self.assertIsNone(cmux_setup.hide_app(for_s=0.2))


class QuittingCmuxSticks(unittest.TestCase):
    """The sweep lists workspaces every few seconds. Repairing on that
    brought a cmux the user had just quit straight back, every time,
    within seconds - they could not try the product without it. Now only
    something the user asked for relaunches it: a turn, a worker."""

    def runtime(self):
        rt = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
        rt.cmux = "/fake/cmux"
        rt._password = "old"
        rt.places = {}
        rt.transcript = None
        self.attempts = []

        def run(argv, **kwargs):
            self.attempts.append(argv)
            return mock.Mock(returncode=1, stdout="", stderr=CLOSED)
        self.patched = mock.patch("subprocess.run", run)
        return rt

    def test_listing_a_quit_cmux_does_not_bring_it_back(self):
        rt = self.runtime()
        with self.patched, mock.patch("conductor.cmux_runtime.repair") as repair:
            for verb in (("workspace", "list", "--json"), ("list-workspaces",),
                         ("list-windows",), ("read-screen", "--surface", "s"),
                         ("list-pane-surfaces", "--workspace", "w")):
                out = rt._cmux(*verb)
                self.assertEqual(out.returncode, 1, verb)
        repair.assert_not_called()
        self.assertEqual(len(self.attempts), 5, "each asked once, none retried")

    def test_a_turn_or_a_worker_does(self):
        rt = self.runtime()
        with self.patched, mock.patch("conductor.cmux_runtime.repair",
                                      return_value="pw") as repair:
            rt._cmux("send", "--surface", "s", "hey")
            rt._cmux("new-workspace", "--command", "")
            rt._cmux("select-workspace", "--workspace", "w")
        self.assertEqual(repair.call_count, 3)

    def test_the_workers_read_as_gone_meanwhile(self):
        """What the sweep gets from a quit cmux: a failed listing, which
        the caller treats as 'cannot see' - not an empty list, and not a
        relaunch."""
        rt = self.runtime()
        with self.patched, mock.patch("conductor.cmux_runtime.repair") as repair:
            self.assertEqual(rt._workspaces(), [])
            self.assertIsNone(rt._lookup("cond_task_a"))
        repair.assert_not_called()

    def test_the_surface_follows_the_same_rule(self):
        from conductor.cmux_surface import CmuxSurface, CmuxUnavailableError
        s = CmuxSurface(binary="/fake/cmux")

        def run(argv, **kwargs):
            return mock.Mock(returncode=1, stdout="", stderr=CLOSED)
        with mock.patch("subprocess.run", run), \
                mock.patch("conductor.cmux_surface.repair", return_value="pw") as repair:
            with self.assertRaises(CmuxUnavailableError):
                s._run("list-pane-surfaces", "--workspace", "w")
            repair.assert_not_called()
            try:
                s._run("select-workspace", "--workspace", "w")
            except (CmuxUnavailableError, RuntimeError):
                pass
            repair.assert_called_once()


if __name__ == "__main__":
    unittest.main()
