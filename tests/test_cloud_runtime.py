"""The two-phase cloud worker: blind in the cloud, supervised once local.

The property worth protecting is that the blindness is *stated*, never
papered over. A cloud worker we cannot read must not be reported as
finished, failed, or gone - and must not be silently treated as local.

Run with:  python3 -m unittest tests.test_cloud_runtime -v
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from conductor.agent_events import AgentEvent
from conductor.cloud_runtime import SESSION_ID, CloudClaudeRuntime


class FakeLocal:
    """Stands in for TmuxClaudeRuntime once a worker is teleported."""

    def __init__(self) -> None:
        self.adopted: list = []
        self.calls: list = []
        self.handlers: dict = {}
        self.status = "running"

    async def adopt_session(self, task_id, name, cwd, existing=None):
        self.adopted.append((task_id, name, cwd))
        return "adopted"

    async def send(self, sid, message):
        self.calls.append(("send", sid, message))

    async def interrupt(self, sid):
        self.calls.append(("interrupt", sid))

    async def resume(self, sid, cwd=None):
        self.calls.append(("resume", sid))

    async def get_status(self, sid):
        return self.status

    async def reconcile_session(self, sid):
        return "running"

    async def subscribe(self, sid, handler):
        self.handlers.setdefault(sid, []).append(handler)
        return lambda: None

    async def destroy(self, sid):
        self.calls.append(("destroy", sid))


PANE = ("Created cloud session: Session instructions\n"
        "View: https://claude.ai/code/session_01ABCdef?from=cli&m=0\n"
        "Resume with: claude --teleport session_01ABCdef\n")


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.local = FakeLocal()
        # reveal MUST be off here. It defaults on, and this fixture builds
        # a real runtime: with it on, every make_worker() in this file
        # opened an actual browser tab. The tests that care about revealing
        # construct their own runtime or call show() under a patch.
        self.rt = CloudClaudeRuntime(local=self.local, claude_binary="claude",
                                     create_timeout=2.0, reveal=False)
        self.tmux_calls: list = []

    def fake_tmux(self, pane: str = PANE, rc: int = 0):
        def run(*args):
            self.tmux_calls.append(args)
            if args[0] == "capture-pane":
                return mock.Mock(returncode=0, stdout=pane, stderr="")
            if args[0] == "has-session":
                return mock.Mock(returncode=1, stdout="", stderr="")
            return mock.Mock(returncode=rc, stdout="", stderr="tmux boom")
        return run

    def opens(self, run) -> list:
        """Every `open` the code asked for, in order."""
        return [c.args[0] for c in run.call_args_list
                if c.args and c.args[0] and c.args[0][0] == "open"]

    async def make_worker(self):
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()):
            return await self.rt.create_session("task_a", "/repo", "do it")


class CreatingInTheCloud(Base):
    async def test_the_session_id_is_read_out_of_the_pane(self):
        sid = await self.make_worker()
        self.assertEqual(sid, "session_01ABCdef")
        self.assertIn("claude.ai/code/session_01ABCdef",
                      self.rt.url_for(sid))

    async def test_it_launches_through_a_tty_because_cloud_demands_one(self):
        """--cloud refuses a pipe and says so; a detached pane is a TTY."""
        await self.make_worker()
        launch = next(a for a in self.tmux_calls if a[0] == "new-session")
        command = launch[-1]
        self.assertIn("--cloud", command)
        self.assertIn("do it", command)

    async def test_the_pane_outlives_the_command_that_prints_the_id(self):
        """`claude --cloud` prints the id and exits; a tmux session whose
        command exits takes the pane with it, and the id with that. This
        was a real failure the fake could not see."""
        await self.make_worker()
        command = next(a for a in self.tmux_calls
                       if a[0] == "new-session")[-1]
        self.assertIn("sleep", command)

    async def test_a_stale_api_key_is_dropped_from_the_launch(self):
        """It takes the API path and 401s; the claude.ai login is what a
        cloud session runs on."""
        await self.make_worker()
        command = next(a for a in self.tmux_calls
                       if a[0] == "new-session")[-1]
        self.assertIn("env -u ANTHROPIC_API_KEY", command)

    async def test_the_starter_pane_is_cleaned_up(self):
        await self.make_worker()
        self.assertTrue(any(a[0] == "kill-session" for a in self.tmux_calls))

    async def test_no_tty_is_reported_as_itself(self):
        pane = "Error: --cloud requires an interactive terminal."
        with mock.patch.object(self.rt, "_tmux",
                               side_effect=self.fake_tmux(pane)):
            with self.assertRaises(RuntimeError) as caught:
                await self.rt.create_session("task_b", "/repo", "go")
        self.assertIn("no TTY", str(caught.exception))

    async def test_one_task_one_cloud_session(self):
        await self.make_worker()
        with self.assertRaises(RuntimeError):
            await self.rt.create_session("task_a", "/repo", "again")


class BlindUntilTeleported(Base):
    async def test_an_unreadable_worker_is_not_called_gone(self):
        """Reporting 'disconnected' for a session we merely cannot read is
        how a live worker gets replaced underneath the user."""
        sid = await self.make_worker()
        self.assertEqual(await self.rt.get_status(sid), "running")
        self.assertFalse(self.rt.is_readable(sid))

    async def test_reconcile_says_uncertain_never_missing(self):
        sid = await self.make_worker()
        self.assertEqual(await self.rt.reconcile_session(sid), "unreachable")

    async def test_sending_works_while_cloud_side(self):
        sid = await self.make_worker()
        with mock.patch.object(self.rt, "_claude",
                               new=mock.AsyncMock(return_value="{}")) as c:
            await self.rt.send(sid, "also fix the tests")
        args = c.await_args.args
        self.assertIn("--cloud", args)
        self.assertIn("--print", args)
        self.assertIn("also fix the tests", args)

    async def test_interrupt_refuses_rather_than_pretending(self):
        """There is no cloud-side interrupt. Saying so beats a no-op that
        looks like it worked."""
        sid = await self.make_worker()
        with self.assertRaises(RuntimeError) as caught:
            await self.rt.interrupt(sid)
        self.assertIn("teleport it first", str(caught.exception))

    async def test_an_unknown_session_is_disconnected(self):
        self.assertEqual(await self.rt.get_status("session_nope"),
                         "disconnected")


class TeleportHandsOver(Base):
    async def test_teleport_does_not_wait_on_a_transcript_that_never_comes(self):
        """The original design handed the pane to the tmux runtime, which
        blocks until a transcript file appears. A teleported session writes
        none - verified against a real session - so it hung until timeout."""
        sid = await self.make_worker()
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()):
            await self.rt.teleport(sid)
        self.assertTrue(self.rt.is_readable(sid))
        self.assertEqual(self.local.adopted, [])
        launch = [a for a in self.tmux_calls if a[0] == "new-session"][-1]
        command = launch[-1]
        self.assertIn("--teleport", command)
        self.assertIn(sid, command)
        # A stale key 401s the teleport too, and the symptom is a missing
        # transcript rather than an auth error.
        self.assertIn("env -u ANTHROPIC_API_KEY", command)

    async def test_after_teleport_input_goes_into_the_pane(self):
        """No transcript means no routing through the local runtime: the
        pane is the session, so typing into it is how a message lands."""
        sid = await self.make_worker()
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()):
            await self.rt.teleport(sid)
            await self.rt.send(sid, "hello")
            await self.rt.interrupt(sid)
        typed = [a for a in self.tmux_calls if a[0] == "send-keys"]
        self.assertTrue(any("hello" in a for a in typed))
        self.assertTrue(any("Escape" in a for a in typed))
        self.assertEqual(self.local.calls, [])

    async def test_status_after_teleport_comes_from_the_provider_feed(self):
        sid = await self.make_worker()
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()):
            await self.rt.teleport(sid)
        rows = [{"pid": 1, "cwd": "/repo", "status": "idle"}]
        with mock.patch("conductor.agent_feed.sessions",
                        new=mock.AsyncMock(return_value=rows)):
            self.assertEqual(await self.rt.get_status(sid), "idle")

    async def test_a_teleported_worker_absent_from_the_feed_is_not_gone(self):
        sid = await self.make_worker()
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()):
            await self.rt.teleport(sid)
        with mock.patch("conductor.agent_feed.sessions",
                        new=mock.AsyncMock(return_value=[])):
            self.assertEqual(await self.rt.get_status(sid), "running")

    async def test_teleport_says_what_it_did_and_did_not_buy(self):
        """The event has to name the limit: readable and typeable, but no
        completions or approvals, because the transcript stays remote."""
        sid = await self.make_worker()
        seen = []
        await self.rt.subscribe(sid, seen.append)
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()):
            await self.rt.teleport(sid)
        note = next(e for e in seen if e.type == "progress")
        self.assertIn("server-side", note.summary)

    async def test_teleporting_twice_is_harmless(self):
        """The second call is a no-op, not a second pane racing the first
        for the same session."""
        sid = await self.make_worker()
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()):
            await self.rt.teleport(sid)
            before = len([a for a in self.tmux_calls if a[0] == "new-session"])
            await self.rt.teleport(sid)
            after = len([a for a in self.tmux_calls if a[0] == "new-session"])
        self.assertEqual(before, after)


class IdParsing(unittest.TestCase):
    def test_both_id_shapes_are_recognised(self):
        self.assertEqual(SESSION_ID.search("x session_01ABC y").group(1),
                         "session_01ABC")
        self.assertEqual(SESSION_ID.search("x cse_9zZ y").group(1), "cse_9zZ")

    def test_the_real_pane_output_parses(self):
        self.assertEqual(SESSION_ID.search(PANE).group(1), "session_01ABCdef")

    def test_unrelated_text_matches_nothing(self):
        self.assertIsNone(SESSION_ID.search("no identifiers here"))


if __name__ == "__main__":
    unittest.main()


class RevealsWhereItIsWorking(Base):
    """A cloud worker has no window here. Without this it runs somewhere
    the user cannot see, which is the whole objection to the mode."""

    async def test_starting_a_worker_shows_it(self):
        """A cloud worker has no window here; a silent start is work
        happening where the user cannot see it."""
        rt = CloudClaudeRuntime(local=self.local, claude_binary="claude",
                                create_timeout=2.0, reveal=True)
        # returncode=0 because that is what `open` really returns; a bare
        # Mock made the app branch look like a failure and fall through to
        # the browser, opening two windows.
        with mock.patch.object(rt, "_tmux", side_effect=self.fake_tmux()), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run:
            await rt.create_session("task_a", "/repo", "do it")
        opened = self.opens(run)
        self.assertEqual(len(opened), 1)
        self.assertIn("session_01ABCdef", " ".join(opened[0]))

    async def test_the_automatic_reveal_fires_once_per_session(self):
        """What went wrong the first time was not opening it - it was
        opening it again. Probing this repeatedly left 38 tabs."""
        sid = await self.make_worker()
        with mock.patch("conductor.cloud_runtime."
                        "_desktop_app_handles_code_sessions",
                        return_value=False), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run:
            for _ in range(5):
                self.rt.show(sid, force=False)
        self.assertEqual(len(self.opens(run)), 1,
                         "the automatic reveal repeated itself")

    async def test_an_explicit_tap_always_opens(self):
        """Dedupe must not outlive the tab: if the user closes it, tapping
        the card has to bring it back rather than report success and do
        nothing."""
        sid = await self.make_worker()
        with mock.patch("conductor.cloud_runtime."
                        "_desktop_app_handles_code_sessions",
                        return_value=False), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run:
            self.rt.show(sid)
            self.rt.show(sid)
        self.assertEqual(len(self.opens(run)), 2)

    async def test_reveal_can_be_turned_off(self):
        rt = CloudClaudeRuntime(local=self.local, claude_binary="claude",
                                create_timeout=2.0, reveal=False)
        with mock.patch.object(rt, "_tmux", side_effect=self.fake_tmux()), \
             mock.patch("subprocess.run") as run:
            await rt.create_session("task_a", "/repo", "do it")
        self.assertEqual(self.opens(run), [])

    async def test_the_browser_is_used_when_the_app_cannot_show_it(self):
        """Verified on this machine: the desktop app registers claude://
        but has no route to a code session, and lands on its sign-in
        screen. So the web is the path that works, not a fallback."""
        sid = await self.make_worker()
        with mock.patch("conductor.cloud_runtime."
                        "_desktop_app_handles_code_sessions",
                        return_value=False), \
             mock.patch("subprocess.run") as run:
            where = self.rt.show(sid)
        self.assertEqual(where, "web")
        args = run.call_args_list[-1].args[0]
        self.assertEqual(args[0], "open")
        self.assertNotIn("-a", args)

    async def test_the_app_is_used_when_asked_for_and_available(self):
        sid = await self.make_worker()
        self.rt.prefer_app = True
        with mock.patch("conductor.cloud_runtime."
                        "_desktop_app_handles_code_sessions",
                        return_value=True), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run:
            where = self.rt.show(sid)
        self.assertEqual(where, "app")
        opened = run.call_args_list[-1].args[0]
        self.assertTrue(opened[1].startswith("claude://code/"),
                        "the app needs its own scheme, not a web address")

    async def test_the_app_never_opens_alongside_the_browser(self):
        """It opened both: the app branch ran, its result was not a clean
        zero, and it fell through. In production `open -a Claude` returns 0
        even when the app only shows its sign-in screen, so that path would
        have reported success and stranded the user there."""
        sid = await self.make_worker()
        self.rt.prefer_app = True
        with mock.patch("conductor.cloud_runtime."
                        "_desktop_app_handles_code_sessions",
                        return_value=True), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run:
            self.rt.show(sid)
        self.assertEqual(len(self.opens(run)), 1)

    async def test_the_browser_is_preferred_because_only_it_addresses_one(self):
        """Claude for Mac registers new, needs-input and
        continue?session=last - no route takes a session id, so
        claude://code/<id> opens the app and lands wherever it already was.
        Observed as "the exact session didn't open"."""
        self.assertFalse(self.rt.prefer_app)

    async def test_turning_the_app_off_sends_everything_to_the_browser(self):
        """`open` returns 0 whether or not the app can show anything, so a
        signed-out app cannot be detected - this switch is the way out."""
        rt = CloudClaudeRuntime(local=self.local, claude_binary="claude",
                                create_timeout=2.0, reveal=False,
                                prefer_app=False)
        rt.workers["s"] = mock.Mock(url="https://claude.ai/code/s",
                                    teleported=False, pane="")
        with mock.patch("conductor.cloud_runtime."
                        "_desktop_app_handles_code_sessions",
                        return_value=True), \
             mock.patch("subprocess.run") as run:
            self.assertEqual(rt.show("s"), "web")
        self.assertNotIn("claude://code/s",
                         " ".join(run.call_args_list[-1].args[0]))

    async def test_show_works_from_only_a_session_id(self):
        """Nothing recorded is required: the URL is derivable."""
        with mock.patch("conductor.cloud_runtime."
                        "_desktop_app_handles_code_sessions",
                        return_value=False), \
             mock.patch("subprocess.run") as run:
            self.rt.show("session_unknown")
        self.assertIn("claude.ai/code/session_unknown",
                      " ".join(run.call_args_list[-1].args[0]))


class TappingTheCardOpensTheSession(unittest.IsolatedAsyncioTestCase):
    """A cloud worker has no window here, so its card has to lead somewhere.

    Tapping it opens the session's own page - the Claude app when that can
    show one, the web otherwise. Deliberately not a teleport: that spends a
    minute building a local pane, and someone tapping a card asked to look
    at the work, not to move it.
    """

    def fake_runtime(self, readable: bool):
        rt = mock.AsyncMock()
        rt.create_session = mock.AsyncMock(return_value="sess_cloud")
        rt.get_status = mock.AsyncMock(return_value="running")
        rt.subscribe = mock.AsyncMock(return_value=lambda: None)
        rt.show = mock.Mock(return_value="web")
        rt.is_readable = mock.Mock(return_value=readable)
        rt.teleport = mock.AsyncMock()
        rt.transcript = None
        return rt

    def conductor_with(self, runtime):
        import tempfile
        from conductor.global_conductor import GlobalConductor
        from conductor.testing import FakeWorkspaceManager
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "code" / "proj" / ".git").mkdir(parents=True)
        return GlobalConductor(
            home=base / "home", runtime=runtime,
            search_roots=[base / "code"],
            workspace_factory=lambda p: FakeWorkspaceManager())

    async def test_focus_reveals_a_cloud_worker_instead_of_teleporting(self):
        runtime = self.fake_runtime(readable=False)
        c = self.conductor_with(runtime)
        pid = c.locator.register(Path(self.tmp.name) / "code" / "proj").id
        task = await c.create_task("Look at me", "g", project_id=pid)
        try:
            await c.focus_task(task.id)
        except Exception:
            pass                       # surfaces are fake here
        runtime.show.assert_called_once()
        runtime.teleport.assert_not_awaited()
        self.tmp.cleanup()

    async def test_an_already_local_worker_is_not_sent_to_the_web(self):
        runtime = self.fake_runtime(readable=True)
        c = self.conductor_with(runtime)
        pid = c.locator.register(Path(self.tmp.name) / "code" / "proj").id
        task = await c.create_task("Local one", "g", project_id=pid)
        try:
            await c.focus_task(task.id)
        except Exception:
            pass
        runtime.show.assert_not_called()
        self.tmp.cleanup()

    async def test_a_runtime_with_no_show_is_unaffected(self):
        """The local runtime has no show(); focus must behave as before."""
        runtime = mock.AsyncMock(spec=["create_session", "send",
                                       "get_status", "subscribe", "destroy",
                                       "interrupt", "resume"])
        runtime.create_session = mock.AsyncMock(return_value="sess_plain")
        runtime.get_status = mock.AsyncMock(return_value="running")
        runtime.subscribe = mock.AsyncMock(return_value=lambda: None)
        c = self.conductor_with(runtime)
        pid = c.locator.register(Path(self.tmp.name) / "code" / "proj").id
        task = await c.create_task("Plain", "g", project_id=pid)
        try:
            await c.focus_task(task.id)
        except Exception:
            pass
        self.tmp.cleanup()


class BootDialogsDoNotBlockTheLaunch(Base):
    """The launch pane hits the same dialogs a local worker does, and
    nobody is watching it either. It sat on "Is this a project you trust?"
    until the timeout and reported only that no id had appeared."""

    TRUST = ("Accessing workspace: /w\n"
             "Quick safety check: Is this a project you created or one you "
             "trust?\n  1. Yes, I trust this folder\n  2. No, exit\n")

    async def test_a_trust_prompt_is_answered(self):
        panes = [self.TRUST, self.TRUST, PANE]

        def tmux(*args):
            self.tmux_calls.append(args)
            if args[0] == "capture-pane":
                return mock.Mock(returncode=0,
                                 stdout=panes.pop(0) if panes else PANE,
                                 stderr="")
            if args[0] == "has-session":
                return mock.Mock(returncode=1, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        self.rt.create_timeout = 12.0     # three polls, one per second
        with mock.patch.object(self.rt, "_tmux", side_effect=tmux), \
             mock.patch("subprocess.run"):
            sid = await self.rt.create_session("task_t", "/repo", "go")
        self.assertEqual(sid, "session_01ABCdef")
        self.assertTrue(any(a[0] == "send-keys" and "Enter" in a
                            for a in self.tmux_calls))

    async def test_the_timeout_says_what_the_pane_showed(self):
        """"no session id appeared" alone hides the reason, which is
        usually a dialog sitting right there in the pane."""
        with mock.patch.object(self.rt, "_tmux",
                               side_effect=self.fake_tmux("Some other wall")), \
             mock.patch("subprocess.run"):
            with self.assertRaises(RuntimeError) as caught:
                await self.rt.create_session("task_t", "/repo", "go")
        self.assertIn("The pane said", str(caught.exception))
        self.assertIn("Some other wall", str(caught.exception))


class NotificationsForATeleportedWorker(Base):
    """A cloud session keeps no local record - verified by making a
    teleported one answer locally and finding nothing under ~/.claude - so
    the usual event stream does not exist for it.

    What does exist is the provider's session feed. A teleported pane shows
    up there, and running -> idle is the end of a turn. That is a
    completion without screen scraping; only the summary comes off the
    pane, and a bad read costs the summary, never the notification.
    """

    async def teleported(self):
        sid = await self.make_worker()
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()):
            await self.rt.teleport(sid)
        return sid, self.rt.workers[sid]

    async def drive(self, worker, statuses, seen):
        """Feed the watcher a status sequence, one poll each."""
        self.rt.poll_seconds = 0.001
        rows = [[{"pid": 1, "cwd": worker.working_directory, "status": s}]
                for s in statuses]
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()), \
             mock.patch("conductor.agent_feed.sessions",
                        new=mock.AsyncMock(side_effect=rows)):
            for _ in statuses:
                await asyncio.sleep(0.01)
        return seen

    async def test_running_then_idle_is_a_completion(self):
        sid, worker = await self.teleported()
        seen = []
        await self.rt.subscribe(sid, seen.append)
        worker.last_status = "running"
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()), \
             mock.patch("conductor.agent_feed.sessions",
                        new=mock.AsyncMock(return_value=[
                            {"pid": 1, "cwd": worker.working_directory,
                             "status": "idle"}])):
            self.rt.poll_seconds = 0.001
            await asyncio.sleep(0.05)
        self.assertIn("completed", [e.type for e in seen])

    async def test_staying_idle_is_not_a_completion(self):
        """Only the transition counts. A worker that was already resting
        has not just finished anything."""
        sid, worker = await self.teleported()
        seen = []
        await self.rt.subscribe(sid, seen.append)
        worker.last_status = "idle"
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()), \
             mock.patch("conductor.agent_feed.sessions",
                        new=mock.AsyncMock(return_value=[
                            {"pid": 1, "cwd": worker.working_directory,
                             "status": "idle"}])):
            self.rt.poll_seconds = 0.001
            await asyncio.sleep(0.05)
        self.assertNotIn("completed", [e.type for e in seen])

    async def test_an_unlisted_session_is_not_a_completion(self):
        """Absent from the feed means not listed yet, not finished."""
        sid, worker = await self.teleported()
        seen = []
        await self.rt.subscribe(sid, seen.append)
        worker.last_status = "running"
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()), \
             mock.patch("conductor.agent_feed.sessions",
                        new=mock.AsyncMock(return_value=[])):
            self.rt.poll_seconds = 0.001
            await asyncio.sleep(0.05)
        self.assertNotIn("completed", [e.type for e in seen])

    async def test_a_failing_poll_is_not_a_finished_turn(self):
        sid, worker = await self.teleported()
        seen = []
        await self.rt.subscribe(sid, seen.append)
        worker.last_status = "running"
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()), \
             mock.patch("conductor.agent_feed.sessions",
                        new=mock.AsyncMock(side_effect=RuntimeError("boom"))):
            self.rt.poll_seconds = 0.001
            await asyncio.sleep(0.05)
        self.assertNotIn("completed", [e.type for e in seen])

    REAL_PANE = ("❯ Fix the race\n"
                 "⏺ Fixed the race in session.py and pushed\n"
                 "  the branch agent/task_auth.\n"
                 "✻ Brewed for 4s · done 12:00 AM\n"
                 "────────────────────────\n"
                 "❯ \n"
                 "  ⏵⏵ auto mode on (shift+tab to cycle)\n")

    async def test_the_summary_is_the_answer_and_only_the_answer(self):
        sid, worker = await self.teleported()
        with mock.patch.object(self.rt, "_tmux",
                               side_effect=self.fake_tmux(self.REAL_PANE)):
            said = self.rt._pane_tail(worker)
        self.assertEqual(
            said, "Fixed the race in session.py and pushed the branch "
                  "agent/task_auth.")

    async def test_the_spinner_line_is_not_mistaken_for_an_answer(self):
        """Every spinner face has to count as the end of the block. One
        wrong codepoint put "Brewed for 4s · done" in the card as though
        the worker had said it - which is exactly what happened."""
        sid, worker = await self.teleported()
        for face in ("✻", "✽", "✳", "✱"):
            pane = f"⏺ Did the thing.\n{face} Brewed for 4s · done\n❯ \n"
            with mock.patch.object(self.rt, "_tmux",
                                   side_effect=self.fake_tmux(pane)):
                self.assertEqual(self.rt._pane_tail(worker), "Did the thing.",
                                 f"{face} leaked into the summary")

    async def test_only_the_last_answer_is_quoted(self):
        sid, worker = await self.teleported()
        pane = ("⏺ First answer.\n✻ done\n❯ next\n"
                "⏺ Second answer.\n✻ done\n❯ \n")
        with mock.patch.object(self.rt, "_tmux",
                               side_effect=self.fake_tmux(pane)):
            self.assertEqual(self.rt._pane_tail(worker), "Second answer.")

    async def test_a_pane_with_no_marker_still_yields_something(self):
        sid, worker = await self.teleported()
        with mock.patch.object(self.rt, "_tmux",
                               side_effect=self.fake_tmux("plain output\n")):
            self.assertEqual(self.rt._pane_tail(worker), "plain output")

    async def test_pane_furniture_alone_yields_nothing(self):
        sid, worker = await self.teleported()
        with mock.patch.object(
                self.rt, "_tmux",
                side_effect=self.fake_tmux("────────\n❯ \n⏵⏵ auto mode on\n")):
            self.assertEqual(self.rt._pane_tail(worker), "")

    async def test_destroy_stops_the_watcher(self):
        sid, worker = await self.teleported()
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()):
            await self.rt.destroy(sid)
        self.assertTrue(worker.finished)


class TheSessionUrlReachesABrowser(Base):
    """A bare `open` does not get a session onto the screen.

    Claude for Mac claims claude.ai as universal links, so macOS hands it
    the URL, and the app has no route that takes a session id - it lands on
    whatever it was already showing and no tab ever appears. Observed as
    "the exact session didn't open up in the app", and it was not the app's
    deep links at fault, it was the interception.
    """

    async def test_the_browser_is_named_rather_than_left_to_the_os(self):
        sid = await self.make_worker()
        with mock.patch("conductor.cloud_runtime."
                        "_desktop_app_handles_code_sessions",
                        return_value=False), \
             mock.patch("conductor.cloud_runtime._default_browser",
                        return_value="com.google.chrome"), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run:
            self.rt.show(sid)
        args = run.call_args_list[-1].args[0]
        self.assertEqual(args[:3], ["open", "-b", "com.google.chrome"])
        self.assertIn("claude.ai/code/", args[3])

    async def test_an_unknown_default_falls_back_to_plain_open(self):
        sid = await self.make_worker()
        with mock.patch("conductor.cloud_runtime."
                        "_desktop_app_handles_code_sessions",
                        return_value=False), \
             mock.patch("conductor.cloud_runtime._default_browser",
                        return_value=""), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run:
            self.rt.show(sid)
        self.assertEqual(run.call_args_list[-1].args[0][0], "open")
        self.assertNotIn("-b", run.call_args_list[-1].args[0])

    async def test_a_browser_that_refuses_falls_back(self):
        sid = await self.make_worker()
        results = [mock.Mock(returncode=1), mock.Mock(returncode=0),
                   mock.Mock(returncode=0)]
        with mock.patch("conductor.cloud_runtime."
                        "_desktop_app_handles_code_sessions",
                        return_value=False), \
             mock.patch("conductor.cloud_runtime._default_browser",
                        return_value="com.example.gone"), \
             mock.patch("subprocess.run", side_effect=results) as run:
            self.assertEqual(self.rt.show(sid), "web")
        self.assertEqual(len(self.opens(run)), 2,
                         "a refused browser must still reach a fallback")


class LookingInsideACloudWorker(Base):
    """peek() is the only supported way to see inside a cloud session.

    Everything else was tried and refused: --output-format stream-json is
    rejected for --cloud, --resume will not take a cloud id, there is no
    follow flag, `claude agents --json` lists local sessions only, and the
    desktop app caches nothing - IndexedDB and Local Storage hold no
    session ids at all.

    What works is teleporting into a throwaway checkout: it brings the
    conversation down as it stands, server-side turns included, and does
    not mutate it. Local turns are already known not to propagate back,
    which is a limitation for working and exactly what makes this safe to
    read.
    """

    RESUMED = ("❯ ping\n⏺ pong\n⏺ Session resumed\n"
               "  tmux detected · scroll with PgUp\n────────\n❯ \n")

    def peek_tmux(self, pane: str):
        def tmux(*args):
            self.tmux_calls.append(args)
            if args[0] == "capture-pane":
                return mock.Mock(returncode=0, stdout=pane, stderr="")
            if args[0] == "has-session":
                return mock.Mock(returncode=1, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        return tmux

    async def test_it_reports_the_last_thing_the_worker_said(self):
        with mock.patch.object(self.rt, "_tmux",
                               side_effect=self.peek_tmux(self.RESUMED)), \
             mock.patch("subprocess.run"):
            out = await self.rt.peek("session_x", timeout=6)
        self.assertTrue(out["ok"])
        self.assertEqual(out["said"], "pong")

    async def test_the_clis_own_lines_are_not_the_summary(self):
        """"Session resumed" arrives on the same bullet as an answer, so
        without care it becomes the summary of a worker that has not
        spoken since. It did, on the first real run."""
        pane = "⏺ Session resumed\n  tmux detected · scroll\n────────\n❯ \n"
        with mock.patch.object(self.rt, "_tmux",
                               side_effect=self.peek_tmux(pane)), \
             mock.patch("subprocess.run"):
            out = await self.rt.peek("session_x", timeout=6)
        self.assertNotIn("Session resumed", out["said"])

    async def test_a_waiting_worker_reports_its_question(self):
        pane = ("⏺ I need to install a package.\n"
                "Do you want me to run npm install?\n"
                "  1. Yes\n  2. No, exit\n⏺ Session resumed\n")
        with mock.patch.object(self.rt, "_tmux",
                               side_effect=self.peek_tmux(pane)), \
             mock.patch("subprocess.run"):
            out = await self.rt.peek("session_x", timeout=6)
        self.assertIn("npm install", out["asked"])

    async def test_the_probe_cleans_up_after_itself(self):
        with mock.patch.object(self.rt, "_tmux",
                               side_effect=self.peek_tmux(self.RESUMED)), \
             mock.patch("subprocess.run"):
            await self.rt.peek("session_x", timeout=6)
        self.assertTrue(any(a[0] == "kill-session" for a in self.tmux_calls),
                        "the peek pane was left running")

    async def test_a_failed_launch_is_not_news_about_the_worker(self):
        def tmux(*args):
            if args[0] == "has-session":
                return mock.Mock(returncode=1, stdout="", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="no tmux")
        with mock.patch.object(self.rt, "_tmux", side_effect=tmux), \
             mock.patch("subprocess.run"):
            out = await self.rt.peek("session_x", timeout=6)
        self.assertFalse(out["ok"])
        self.assertEqual(out["said"], "")


class ACloudWorkerNoticesItsOwnTurns(Base):
    """Cloud sessions push nothing, so noticing means going to look.

    peek costs a process and several seconds, so the poll is minutes-scale:
    the difference between finding out on your own and having to ask, not a
    live feed. Verified against a real session, which notified in 34s and
    55s on two runs.
    """

    async def worker(self, **peeks):
        sid = await self.make_worker()
        w = self.rt.workers[sid]
        self.rt.cloud_poll_seconds = 0.01
        seen = []
        await self.rt.subscribe(sid, seen.append)
        return sid, w, seen

    async def run_poll(self, w, results, seen, ticks=4):
        with mock.patch.object(self.rt, "peek",
                               new=mock.AsyncMock(side_effect=results)):
            task = asyncio.create_task(self.rt._watch_cloud(w))
            for _ in range(ticks):
                await asyncio.sleep(0.03)
            w.finished = True
            task.cancel()
        return seen

    async def test_a_new_answer_is_a_completion(self):
        sid, w, seen = await self.worker()
        await self.run_poll(w, [{"ok": True, "said": "Fixed it.",
                                 "asked": ""}] * 4, seen)
        done = [e for e in seen if e.type == "completed"]
        self.assertTrue(done)
        self.assertEqual(done[0].summary, "Fixed it.")

    async def test_the_same_answer_does_not_notify_twice(self):
        """It is a poll: the answer is still there on the next look."""
        sid, w, seen = await self.worker()
        await self.run_poll(w, [{"ok": True, "said": "Fixed it.",
                                 "asked": ""}] * 6, seen, ticks=6)
        self.assertEqual(len([e for e in seen if e.type == "completed"]), 1)

    async def test_a_question_outranks_an_answer(self):
        """A cloud session cannot bypass permissions, so stalling on a
        question is the common way one goes quiet."""
        sid, w, seen = await self.worker()
        await self.run_poll(w, [{"ok": True, "said": "thinking",
                                 "asked": "Run npm install?"}] * 4, seen)
        kinds = [e.type for e in seen]
        self.assertIn("needs_input", kinds)
        self.assertNotIn("completed", kinds)

    async def test_a_failed_look_says_nothing(self):
        sid, w, seen = await self.worker()
        await self.run_poll(w, [{"ok": False, "said": "", "asked": ""}] * 4,
                            seen)
        self.assertEqual([e for e in seen if e.type in
                          ("completed", "needs_input")], [])

    async def test_an_exception_while_looking_says_nothing(self):
        sid, w, seen = await self.worker()
        await self.run_poll(w, RuntimeError("boom"), seen)
        self.assertEqual([e for e in seen if e.type == "completed"], [])

    async def test_teleporting_stops_the_slow_poll(self):
        """The teleported worker has the fast path; two watchers would
        double every notification."""
        sid, w, seen = await self.worker()
        with mock.patch.object(self.rt, "_tmux", side_effect=self.fake_tmux()):
            await self.rt.teleport(sid)
        self.assertTrue(w.teleported)

    async def test_polling_can_be_switched_off(self):
        rt = CloudClaudeRuntime(local=self.local, claude_binary="claude",
                                create_timeout=2.0, reveal=False,
                                cloud_poll_seconds=0)
        with mock.patch.object(rt, "_tmux", side_effect=self.fake_tmux()), \
             mock.patch("subprocess.run"):
            sid = await rt.create_session("task_q", "/repo", "go")
        self.assertIsNone(rt.workers[sid].watcher)
