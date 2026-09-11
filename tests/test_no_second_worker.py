"""One conversation, one process - across a Conductor restart.

Found live, not by reading: after a day of restarts, task d07abc5a had
TWO processes on session b1ca8fe7 - pid 43223, the worker in
`cond_task_d07abc5a`, and pid 47921, `claude --resume b1ca8fe7` in
`cond_resumed_b1ca8fe7` - both running for nearly three hours.

resume() guarded against exactly this, but only by consulting
`self.sessions`, the runtime's in-memory map. A Conductor restart empties
that map while the worker it started keeps running, so the guard passed
and a second process was launched onto a live conversation.

Our own memory is not evidence that nothing is running. The PTY is.

Run with:  python3 -m unittest tests.test_no_second_worker -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from conductor.tmux_runtime import TmuxClaudeRuntime

CWD = "/Users/x/.voice-conductor/workspaces/proj_1/task_d07abc5a"
SESSION = "b1ca8fe7-c525-4c1d-8a9a-cf9e73078913"


class Base(unittest.TestCase):
    def runtime(self, alive: set[str]) -> TmuxClaudeRuntime:
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        rt.sessions = {}
        rt.claude = "/bin/claude"
        rt.startup_timeout = 1.0
        rt.transcript = None
        self.calls: list[tuple] = []

        def tmux(*args):
            self.calls.append(args)
            if args[0] == "has-session":
                name = args[args.index("-t") + 1]
                return mock.Mock(returncode=0 if name in alive else 1,
                                 stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        rt._tmux = tmux
        # The process table vouches for every window the host lists;
        # a test about an empty window says otherwise itself.
        rt._process_alive = lambda sess: sess.name in alive
        rt._watch = lambda sess: asyncio.sleep(0)
        return rt

    def started_a_process(self) -> bool:
        return any(c[0] == "new-session" for c in self.calls)


class ResumingAfterARestart(Base):
    def test_a_running_worker_is_adopted_not_duplicated(self):
        """The map is empty (we restarted); the worker is not."""
        rt = self.runtime(alive={"cond_task_d07abc5a"})
        asyncio.run(rt.resume(SESSION, working_directory=CWD))
        self.assertFalse(self.started_a_process(),
                         "started a second process on a live conversation")
        self.assertIn(SESSION, rt.sessions)
        self.assertEqual(rt.sessions[SESSION].name, "cond_task_d07abc5a")

    def test_an_adopted_worker_is_supervised(self):
        """Adopting without watching would leave the task running blind -
        no turns, no completion, no notifications."""
        rt = self.runtime(alive={"cond_task_d07abc5a"})
        asyncio.run(rt.resume(SESSION, working_directory=CWD))
        self.assertIsNotNone(rt.sessions[SESSION].watcher)

    def test_a_dead_worker_is_still_resumed(self):
        """The fix must not stop resume from doing its job."""
        rt = self.runtime(alive=set())
        asyncio.run(rt.resume(SESSION, working_directory=CWD))
        self.assertTrue(self.started_a_process(),
                        "nothing was resumed and nothing was adopted")

    def test_a_resumed_worker_is_named_where_the_surface_will_look(self):
        """The surface looks for cond_task_<id>. A resume that invented
        cond_resumed_<session> was invisible to it, so the click resumed
        the same conversation a SECOND time - the tab reported another
        Claude Code holding Remote Control, because there was one."""
        rt = self.runtime(alive=set())
        asyncio.run(rt.resume(SESSION, working_directory=CWD))
        made = next(c for c in self.calls if c[0] == "new-session")
        self.assertEqual(made[made.index("-s") + 1], "cond_task_d07abc5a")

    def test_an_empty_window_is_not_a_worker_to_adopt(self):
        """The workspace is listed; the claude inside it has exited (a
        Conductor restart took it, 09:09:25Z on 2026-08-30). Adopting the
        window would have supervised a bare shell and never started the
        process the resume was for."""
        rt = self.runtime(alive={"cond_task_d07abc5a"})
        rt._process_alive = lambda sess: False
        self.assertIsNone(rt._live_worker_in(CWD))
        asyncio.run(rt.resume(SESSION, working_directory=CWD))
        self.assertTrue(self.started_a_process(),
                        "an empty window was adopted as the worker")

    def test_a_checkout_that_is_not_a_task_is_not_guessed_at(self):
        """The name comes from the checkout, so a directory that is not a
        task checkout must not be turned into a session name."""
        rt = self.runtime(alive={"cond_task_d07abc5a"})
        self.assertIsNone(rt._live_worker_in("/Users/x/code/whatever"))

    def test_history_is_not_replayed_as_new_work(self):
        """A worker adopted mid-flight has already finished turns. Reading
        its transcript from the top would announce them as if they had
        just happened."""
        import tempfile
        from pathlib import Path
        from conductor import tmux_runtime
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            folder = projects / tmux_runtime.munge_project_dir(CWD)
            folder.mkdir(parents=True)
            transcript = folder / f"{SESSION}.jsonl"
            transcript.write_text('{"old": "turn"}\n' * 5)
            rt = self.runtime(alive={"cond_task_d07abc5a"})
            with mock.patch.object(tmux_runtime, "CLAUDE_PROJECTS", projects):
                asyncio.run(rt.resume(SESSION, working_directory=CWD))
            sess = rt.sessions[SESSION]
            self.assertEqual(sess.jsonl_path, transcript)
            self.assertEqual(sess.offset, transcript.stat().st_size)


class ANameCollidesOutLoud(Base):
    """`error: could not resume: duplicate session: cond_task_b152b744`,
    twice, and every message to that worker bounced (2026-09-08).

    Found afterwards in `tmux ls`: cond_task_b152b744 had been created on
    Sep 3 and was still listed a week later, an empty window left behind
    by a restart with no claude in it. _live_worker_in was right to
    refuse to adopt it - and then resume created a session under a name
    tmux already had, which tmux refuses. Nobody killed the empty window,
    nobody said the name was the problem, and the error the user got was
    a generic "could not resume".
    """

    def test_an_empty_window_is_closed_so_the_name_can_be_used(self):
        rt = self.runtime(alive={"cond_task_d07abc5a"})
        rt._process_alive = lambda sess: False
        asyncio.run(rt.resume(SESSION, working_directory=CWD))
        killed = [c for c in self.calls if c[0] == "kill-session"]
        self.assertTrue(killed, "the week-old empty window was left there")
        self.assertEqual(killed[0][killed[0].index("-t") + 1],
                         "cond_task_d07abc5a")
        self.assertTrue(self.started_a_process())

    def test_a_name_with_a_live_worker_under_it_is_never_reused(self):
        """The other half of the rule. An empty window is reclaimed; a
        window with a worker in it is not, whatever else we believe -
        closing it would kill the worker, and creating beside it would be
        two processes on one conversation."""
        rt = self.runtime(alive={"cond_task_d07abc5a"})
        verdict = asyncio.run(rt._claim_name("cond_task_d07abc5a", CWD,
                                             "task_d07abc5a"))
        self.assertEqual(verdict, "live")
        self.assertEqual([c for c in self.calls if c[0] == "kill-session"],
                         [], "closed a window with a worker in it")

    def test_a_fresh_worker_is_not_started_over_a_live_one(self):
        """launch_session used to kill whatever held the name, in silence.
        For a name nothing is running under that is the right repair; for
        one that IS running it is a worker killed without a word."""
        from conductor.tmux_runtime import DuplicateSession
        rt = self.runtime(alive={"cond_task_d07abc5a"})
        with self.assertRaises(DuplicateSession):
            asyncio.run(rt.launch_session("task_d07abc5a", CWD,
                                          ["claude"]))
        self.assertFalse(self.started_a_process())

    def test_a_duplicate_name_is_its_own_error_not_a_generic_failure(self):
        """tmux says exactly what is wrong. Until now only the string
        reached the user, wrapped in "could not resume", with nothing
        naming the collision or suggesting what causes it."""
        from conductor.tmux_runtime import DuplicateSession
        rt = self.runtime(alive=set())

        def tmux(*args):
            self.calls.append(args)
            if args[0] == "new-session":
                return mock.Mock(returncode=1, stdout="",
                                 stderr="duplicate session: cond_task_d07abc5a")
            return mock.Mock(returncode=0, stdout="", stderr="")
        rt._tmux = tmux
        with self.assertRaises(DuplicateSession) as caught:
            asyncio.run(rt.resume(SESSION, working_directory=CWD))
        self.assertIn("already taken", str(caught.exception))
        self.assertIn("second conductor", str(caught.exception))

    def test_a_worker_is_launched_knowing_which_task_it_is(self):
        """conductor/computer.py runs as a fresh process out of the
        worker's shell; the environment is the only place it can learn
        whose lease to ask for."""
        rt = self.runtime(alive=set())
        rt.home = "/tmp/home"
        asyncio.run(rt.resume(SESSION, working_directory=CWD))
        made = next(c for c in self.calls if c[0] == "new-session")
        self.assertIn("VOICE_CONDUCTOR_TASK_ID=task_d07abc5a", made)
        self.assertIn("VOICE_CONDUCTOR_HOME=/tmp/home", made)


class WhenCmuxIsGone(unittest.TestCase):
    """Measured by killing cmux with workers running: every worker process
    died with it, and the app left its socket FILE behind - so a dead cmux
    says "Connection refused", not "Socket not found"."""

    def test_a_dead_cmux_is_unavailable_not_a_failed_command(self):
        from conductor.cmux_setup import unavailable_reason
        refused = ("Error: Failed to connect to socket at "
                   "/Users/x/.local/state/cmux/cmux.sock "
                   "(Connection refused, errno 61)")
        self.assertEqual(unavailable_reason(refused), "cmux is not running")

    def test_a_missing_socket_still_counts(self):
        from conductor.cmux_setup import unavailable_reason
        self.assertEqual(unavailable_reason("Error: Socket not found"),
                         "cmux is not running")

    def test_an_ordinary_failure_is_left_alone(self):
        """Otherwise every rejected argument would read as "cmux is gone"
        and the caller would go looking for a dead app."""
        from conductor.cmux_setup import unavailable_reason
        self.assertIsNone(unavailable_reason(
            "Error: invalid_params: Missing or invalid group_id"))

    def test_the_surface_raises_the_unavailable_error(self):
        from conductor.cmux_surface import CmuxSurface, CmuxUnavailableError
        surface = CmuxSurface(binary="/fake/cmux")
        with mock.patch("subprocess.run") as run, \
             mock.patch("conductor.cmux_surface.repair", return_value=""):
            run.return_value = mock.Mock(
                returncode=1, stdout="",
                stderr="Failed to connect to socket (Connection refused)")
            with self.assertRaises(CmuxUnavailableError):
                surface._run("workspace", "list")


if __name__ == "__main__":
    unittest.main()


class TappingANotificationLandsInCmux(unittest.TestCase):
    """Both the notification body and the bell route through focus_task,
    so this is the same click either way.

    It stopped working when routing arrived, and the reason is subtle:
    focus_task chose between "reveal this in the cloud" and "open the
    local surface" by asking which capabilities the runtime offered.
    A local session answers None to is_readable - that means "no", not
    "not readable in the cloud" - so `not readable(...)` was true and the
    cloud branch ran for a worker sitting in a cmux workspace. show()
    then returned nothing, focus_task reported success, and the click
    took the user precisely nowhere.
    """

    def conductor(self, location, show_returns="https://claude.ai/x"):
        from conductor.global_conductor import GlobalConductor
        gc = GlobalConductor.__new__(GlobalConductor)
        gc.runtime = mock.Mock()
        gc.runtime.location_of = lambda sid: location
        gc.runtime.is_readable = lambda sid: None      # local answers None
        gc.runtime.show = mock.Mock(return_value=show_returns)
        gc._emit = lambda *a, **k: None
        gc.surfaces = {}
        return gc

    def focus(self, gc):
        task = mock.Mock(id="task_a", project_id="p",
                         provider_session_id="sess-1", surface=None)
        conductor = mock.Mock()
        conductor._ensure_session = mock.AsyncMock()
        gc._find_task = lambda tid, pid=None: (conductor, task)
        try:
            asyncio.run(gc.focus_task("task_a"))
        except RuntimeError as exc:
            return str(exc)          # "no session surface is configured"
        return ""

    def test_a_local_worker_is_not_sent_to_the_cloud(self):
        gc = self.conductor(location="local")
        reached_the_surface = self.focus(gc)
        gc.runtime.show.assert_not_called()
        self.assertIn("surface", reached_the_surface,
                      "focus returned without ever reaching the surface")

    def test_a_cloud_worker_still_gets_its_page(self):
        gc = self.conductor(location="cloud")
        self.focus(gc)
        gc.runtime.show.assert_called_once()

    def test_revealing_nothing_is_not_revealing(self):
        """show() returning nothing used to count as success, so the click
        was reported as handled while the screen did not change."""
        gc = self.conductor(location="cloud", show_returns=None)
        reached_the_surface = self.focus(gc)
        self.assertIn("surface", reached_the_surface,
                      "an empty reveal was treated as done")

    def test_opening_a_surface_also_shows_it(self):
        """Opening is not showing. A Terminal window arrives in front of
        you, so nobody noticed; a cmux workspace is attached to without
        being selected, so the click opened the right session behind
        whatever the user was looking at."""
        gc = self.conductor(location="local")
        shown = []
        impl = mock.Mock()
        impl.focus = lambda handle: shown.append(handle)
        handle = mock.Mock(native_window_id="WS")
        gc.surfaces = {"cmux": impl}
        gc._surface_impl = lambda h=None: impl
        gc._open_surface = mock.AsyncMock(return_value=handle)
        gc._touch = lambda *a, **k: None
        task = mock.Mock(id="task_a", project_id="p", title="t",
                         provider_session_id="sess-1", surface=None)
        conductor = mock.Mock()
        conductor._ensure_session = mock.AsyncMock()
        conductor.store.get = lambda tid: task
        gc._find_task = lambda tid, pid=None: (conductor, task)
        asyncio.run(gc.focus_task("task_a"))
        self.assertEqual(shown, [handle], "surface opened but never shown")

    def test_a_surface_that_will_not_come_forward_is_not_fatal(self):
        """The session is open either way; a window that will not raise is
        a worse view of it, not a failed click."""
        gc = self.conductor(location="local")
        impl = mock.Mock()
        impl.focus = mock.Mock(side_effect=RuntimeError("no window"))
        gc.surfaces = {"cmux": impl}
        gc._surface_impl = lambda h=None: impl
        gc._open_surface = mock.AsyncMock(return_value=mock.Mock(
            native_window_id="WS"))
        gc._touch = lambda *a, **k: None
        task = mock.Mock(id="task_a", project_id="p", title="t",
                         provider_session_id="sess-1", surface=None)
        conductor = mock.Mock()
        conductor._ensure_session = mock.AsyncMock()
        conductor.store.get = lambda tid: task
        gc._find_task = lambda tid, pid=None: (conductor, task)
        asyncio.run(gc.focus_task("task_a"))          # must not raise


class BothSidesUseOneName(unittest.TestCase):
    """The runtime creates the workspace; the surface has to find it. They
    disagreed, and the disagreement was invisible because both looked
    plausible in isolation:

        runtime:  cond_ + task_c35cd1c6        -> cond_task_c35cd1c6
        surface:  cond_task_ + task_c35cd1c6   -> cond_task_task_c35cd1c6

    Task ids already begin with "task_", so the surface's name matched
    nothing, ever. Every click resumed a duplicate worker instead of
    attaching to the one already running - which the new tab reported as
    another Claude Code holding Remote Control for the conversation.

    My own live test missed it by using a task id of "probe8", which is
    shaped to match the broken formula. Real ids are used here for that
    reason.
    """

    REAL_ID = "task_c35cd1c6"

    def test_the_surface_looks_for_what_the_runtime_made(self):
        from conductor.cmux_surface import CmuxSurface
        from conductor.surfaces import SurfaceRequest
        from conductor.tmux_runtime import session_name

        made = session_name(self.REAL_ID)          # what the runtime creates
        looked_for = []
        surface = CmuxSurface(binary="/fake/cmux")
        surface._workspace_titled = lambda title: looked_for.append(title)
        request = SurfaceRequest(project_id="p", task_id=self.REAL_ID,
                                 title="Open PRs", working_directory="/w",
                                 provider="claude-code",
                                 provider_session_id="sess")
        try:
            surface.create(request)
        except Exception:
            pass                       # only the lookup name matters here
        self.assertIn(made, looked_for,
                      f"surface never looked for {made}; it tried "
                      f"{looked_for}")

    def test_the_name_is_not_double_prefixed(self):
        from conductor.tmux_runtime import session_name
        self.assertEqual(session_name(self.REAL_ID), "cond_task_c35cd1c6")
        self.assertNotIn("task_task", session_name(self.REAL_ID))
