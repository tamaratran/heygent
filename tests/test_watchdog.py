"""The sweep that keeps sessions from wedging silently.

Two failures, both invisible until someone goes looking:

  * a worker parked on a boot dialog (resume picker, trust prompt) is
    waiting for a keypress nobody is there to give, so every message to it
    times out;
  * a task whose PTY has died is still marked "running", so it is counted
    as a live session for ever. One had been running for seventeen hours.

They need opposite treatment - answer the first, tell the truth about the
second - so the sweep does both and neither is left to chance.

Run with:  python3 -m unittest tests.test_watchdog -v
"""

from __future__ import annotations

import asyncio
import time
import tempfile
import unittest
from pathlib import Path

from conductor.observability import ObservabilityBus

from conductor.global_conductor import GlobalConductor
from tests.test_global import FakeCodingAgentRuntime, FakeManagerBackend, \
    FakeWorkspaceManager, make_repo


class SweepableRuntime(FakeCodingAgentRuntime):
    """A runtime that can be told which PTYs exist and which are wedged."""

    def __init__(self) -> None:
        super().__init__()
        self.live: set[str] = set()
        self.wedged: dict[str, str] = {}   # session name -> dialog
        self.keys_sent: list[str] = []

    def live_session_names(self) -> set:
        return set(self.live)

    async def unstick(self) -> list[dict]:
        cleared = []
        for name, dialog in list(self.wedged.items()):
            self.keys_sent.append(name)
            del self.wedged[name]
            cleared.append({"session": name, "cleared": dialog})
        return cleared


class WatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = base / "code"
        make_repo(self.roots / "posely")
        self.runtime = SweepableRuntime()
        self.conductor = GlobalConductor(
            home=base / "home", runtime=self.runtime,
            manager=FakeManagerBackend(), search_roots=[self.roots],
            workspace_factory=lambda project: FakeWorkspaceManager())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def go(self, coro):
        return asyncio.run(coro)

    def status(self, task_id: str) -> str:
        return self.conductor._find_task(task_id)[1].status

    def a_running_task(self, title: str = "Fix login"):
        pid = self.go(self.conductor.handle_action(
            "register_project", {"path": str(self.roots / "posely")}))[
                "project_id"]
        task = self.go(self.conductor.handle_action(
            "create_task", {"project_id": pid, "title": title,
                            "goal": "g"}))
        self.assertEqual(task.status, "running")
        return task

    # -- dead workers --------------------------------------------------
    def test_a_running_task_with_no_pty_becomes_interrupted(self) -> None:
        task = self.a_running_task()
        self.runtime.live = set()            # its window is gone
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [task.id])
        self.assertEqual(self.status(task.id),
                         "interrupted")

    def test_a_running_task_with_a_live_pty_is_left_alone(self) -> None:
        task = self.a_running_task()
        self.runtime.live = {f"cond_task_{task.id}"}
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [])
        self.assertEqual(self.status(task.id), "running")

    # -- the stuck pair ------------------------------------------------
    def test_the_sweep_repairs_a_running_task_with_a_terminal_sidecar(self) -> None:
        """Task=running with a terminal sidecar is the pair a lost
        running-edge leaves (a retire-and-revive in one process, a
        restart starting the next): the worker is muted and the card
        says Running for ever. The event handler heals it when the
        worker speaks; the sweep must heal it when it does not."""
        from conductor.subagent_state import apply_lifecycle
        task = self.a_running_task()
        self.runtime.live = {f"cond_task_{task.id}"}
        self.runtime.worker_process_alive = lambda name, cwd, sid: True
        conductor, _ = self.conductor._find_task(task.id)
        stuck = apply_lifecycle(conductor.subagents.get(task.id) or
                                conductor._subagent_state(
                                    conductor.store.get(task.id)),
                                "completed")
        conductor.subagents.save(stuck)
        # The worker sits at its prompt: the turn it finished is behind
        # the watcher's offset and will never replay.
        self.runtime.statuses[task.provider_session_id] = "idle"
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["reconciled"], [task.id])
        self.assertEqual(conductor.subagents.get(task.id).status, "idle")
        self.assertEqual(self.status(task.id), "waiting_for_user")
        # Healed means healed: the next sweep finds nothing to repair.
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["reconciled"], [])

    def test_a_stuck_pair_mid_turn_revives_without_ending_the_turn(self) -> None:
        """The same pair while the worker is still working: the sidecar
        un-mutes so the coming finish lands, and the Task stays running
        - only an idle prompt says the turn is over."""
        from conductor.subagent_state import apply_lifecycle
        task = self.a_running_task()
        self.runtime.live = {f"cond_task_{task.id}"}
        self.runtime.worker_process_alive = lambda name, cwd, sid: True
        conductor, _ = self.conductor._find_task(task.id)
        stuck = apply_lifecycle(conductor.subagents.get(task.id) or
                                conductor._subagent_state(
                                    conductor.store.get(task.id)),
                                "completed")
        conductor.subagents.save(stuck)
        self.runtime.statuses[task.provider_session_id] = "running"
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["reconciled"], [task.id])
        self.assertEqual(conductor.subagents.get(task.id).status, "working")
        self.assertEqual(self.status(task.id), "running")

    def test_a_listed_window_with_no_process_is_interrupted(self) -> None:
        """Measured 2026-08-30: cond_task_1c2db9f0's claude exited in a
        restart, its cmux workspace stayed listed as a bare shell, and the
        task was a busy worker for forty minutes - one of three slots, so
        create_task refused every new worker."""
        task = self.a_running_task()
        self.runtime.live = {f"cond_task_{task.id}"}
        asked = []
        self.runtime.worker_process_alive = \
            lambda name, cwd, sid: asked.append((name, cwd, sid)) or False
        events = []
        self.conductor.bus.subscribe(
            lambda e: events.append(e) if e.type == "task.worker_gone" else None)
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [task.id])
        self.assertEqual(self.status(task.id), "interrupted")
        self.assertEqual(asked[0][0], f"cond_task_{task.id}")
        self.assertEqual(asked[0][1], task.workspace.path)
        self.assertEqual(events[0].data["window"], "empty")
        # And it no longer holds a worker slot.
        self.assertFalse(self.conductor._worker_busy(
            self.conductor._find_task(task.id)[1]))

    def test_a_listed_window_with_a_live_process_is_left_alone(self) -> None:
        task = self.a_running_task()
        self.runtime.live = {f"cond_task_{task.id}"}
        self.runtime.worker_process_alive = lambda name, cwd, sid: True
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [])
        self.assertEqual(self.status(task.id), "running")

    def test_the_process_question_goes_to_the_worker_s_own_runtime(self) -> None:
        """Behind a RoutingRuntime the first runtime answers unrouted
        calls - Claude's, which looks for claude processes. A Codex
        worker asked about that way has no process, every time."""
        from conductor.routing_runtime import RoutingRuntime
        task = self.a_running_task()
        local = self.runtime
        local.live = {f"cond_task_{task.id}"}
        local.worker_process_alive = lambda name, cwd, sid: False
        codex = FakeCodingAgentRuntime()
        codex.worker_process_alive = lambda name, cwd, sid: True
        self.conductor.runtime = RoutingRuntime(local, providers={"codex": codex})
        conductor, _ = self.conductor._find_task(task.id)
        conductor.store.update(task.id, provider="codex")
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [],
                         "a live Codex worker was ended on Claude's say-so")
        # A provider nothing hosts: no runtime, no opinion, left alone.
        conductor.store.update(task.id, provider="gemini")
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [])
        # Claude's own worker is still asked about, and still ended.
        conductor.store.update(task.id, provider="claude-code")
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [task.id])

    def test_a_failing_process_check_does_not_end_a_listed_worker(self) -> None:
        task = self.a_running_task()
        self.runtime.live = {f"cond_task_{task.id}"}

        def broken(name, cwd, sid):
            raise RuntimeError("no process table")
        self.runtime.worker_process_alive = broken
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [])
        self.assertEqual(self.status(task.id), "running")

    def test_the_watch_probe_runs_off_the_loop(self) -> None:
        """Measured live: app.loop_stalled 5.6-6.7s every 43s, twelve
        times in six minutes, one of which ended the voice socket. Every
        other probe in the sweep was already threaded; this one asked
        cmux per task on the loop itself - a subprocess each, ~0.2s each,
        twenty tasks - and keys, audio and the session all waited."""
        import threading
        task = self.a_running_task()
        self.runtime.live = {f"cond_task_{task.id}"}
        on_main: list[bool] = []

        def ensure_watched(session_id: str, cwd: str) -> bool:
            on_main.append(threading.current_thread()
                           is threading.main_thread())
            return False
        self.runtime.ensure_watched = ensure_watched
        self.go(self.conductor.sweep_stuck())
        self.assertEqual(on_main, [False], "the probe ran on the loop")

    def test_startup_reads_live_workers_before_the_first_sweep(self) -> None:
        """Launching wakes nothing, and the sweep is thirty seconds
        away; an adopted watcher starts at the end of the transcript.
        A worker that finished in that window after a restart was
        never seen - and we restart after every merge."""
        task = self.a_running_task()
        watched: list[str] = []

        def ensure_watched(session_id: str, cwd: str) -> bool:
            watched.append(session_id)
            return True
        self.runtime.ensure_watched = ensure_watched
        report = self.go(self.conductor.startup())
        self.assertEqual(watched, [task.provider_session_id])
        self.assertEqual(report["watched"], [task.id])

    def test_inspecting_a_local_task_does_not_await_a_missing_peek(self):
        """The router answers None for a session whose runtime has no
        peek - every local worker - and None cannot be awaited. It was:
        every inspect_task on a local task logged cloud.peek_failed."""
        task = self.a_running_task()
        self.runtime.peek = lambda session_id: None
        self.runtime.is_readable = lambda session_id: False
        events = []
        self.conductor.bus.subscribe(events.append)
        report = self.go(self.conductor.inspect_task(task.id))
        self.assertNotIn("cloud.peek_failed", [e.type for e in events])
        self.assertNotIn("where", report)

    def test_subagents_are_probed_together_not_in_turn(self) -> None:
        """Nineteen workers were nineteen probes end to end. The Boss asks
        this for "what is everything doing?"."""
        for i in range(3):                    # the fixture's concurrency limit
            self.a_running_task(f"Task {i}")
        real = self.runtime.reconcile_session

        async def slow(session_id: str) -> str:
            await asyncio.sleep(0.1)
            return await real(session_id)
        self.runtime.reconcile_session = slow
        started = time.monotonic()
        views = self.go(self.conductor.subagents())
        elapsed = time.monotonic() - started
        self.assertEqual(len(views), 3)
        # In turn: 0.3s. Together: about 0.1s.
        self.assertLess(elapsed, 0.22, f"probed in turn: {elapsed:.2f}s")

    def test_a_worker_the_listing_missed_is_asked_about_by_name(self) -> None:
        """Measured live: a listing that parsed to no names at all buried
        every worker 30 s after it started, while has-session said each
        was alive. The listing chooses who to ask about; the by-name
        answer decides."""
        task = self.a_running_task()
        self.runtime.live = set()                        # names nobody
        self.runtime.session_alive = \
            lambda name: name == f"cond_task_{task.id}"  # but it is there
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [])
        self.assertEqual(self.status(task.id), "running")

    def test_a_live_process_saves_a_worker_the_host_forgot(self) -> None:
        """Measured 2026-08-31: a relaunch's first sweep ran before the
        restarted host listed anything, has-session named nobody, and a
        worker whose claude was alive the whole time was marked
        interrupted - the user had to ask for a resume by voice."""
        task = self.a_running_task()
        self.runtime.live = set()
        self.runtime.session_alive = lambda name: False
        asked = []
        self.runtime.worker_process_alive = \
            lambda name, cwd, sid: asked.append((name, cwd, sid)) or True
        events = []
        self.conductor.bus.subscribe(
            lambda e: events.append(e)
            if e.type == "runtime.session_process_alive" else None)
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [])
        self.assertEqual(self.status(task.id), "running")
        self.assertTrue(asked[0][0].startswith("cond_"))
        self.assertEqual(asked[0][1], task.workspace.path)
        self.assertEqual(asked[0][2], task.provider_session_id)
        self.assertEqual(events[0].data["window"], "gone")

    def test_a_failing_process_check_does_not_save_a_gone_worker(self) -> None:
        """Both host accounts already said gone; only a clear "yes"
        from the process table overrules them."""
        task = self.a_running_task()
        self.runtime.live = set()
        self.runtime.session_alive = lambda name: False

        def broken(name, cwd, sid):
            raise RuntimeError("no process table")
        self.runtime.worker_process_alive = broken
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [task.id])
        self.assertEqual(self.status(task.id), "interrupted")

    def test_a_worker_gone_by_both_accounts_is_interrupted(self) -> None:
        task = self.a_running_task()
        self.runtime.live = set()
        self.runtime.session_alive = lambda name: False
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [task.id])

    def test_a_failing_second_opinion_does_not_save_or_break_the_sweep(
            self) -> None:
        task = self.a_running_task()
        self.runtime.live = set()

        def broken(name):
            raise OSError("cmux is not answering")
        self.runtime.session_alive = broken
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [task.id])

    def test_a_dormant_task_is_not_killed_for_having_no_pty(self) -> None:
        """Only "running" claims a worker. Startup no longer wakes anything,
        so a resting task legitimately has no window and must survive the
        sweep - otherwise every dormant session gets marked interrupted."""
        task = self.a_running_task()
        self.conductor._find_task(task.id)[0].store.update(
            task.id, status="waiting_for_user")
        self.runtime.live = set()
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [])
        self.assertEqual(self.status(task.id),
                         "waiting_for_user")

    def test_the_correction_is_announced(self) -> None:
        seen = []
        self.conductor.bus.subscribe(lambda e: seen.append(e.type))
        task = self.a_running_task()
        self.runtime.live = set()
        self.go(self.conductor.sweep_stuck())
        self.assertIn("task.worker_gone", seen)

    def test_an_interrupted_task_is_still_resumable(self) -> None:
        """The point of saying interrupted rather than hiding the row: the
        work is unfinished and reachable, not vanished."""
        task = self.a_running_task()
        self.runtime.live = set()
        self.go(self.conductor.sweep_stuck())
        presence = self.conductor._presence(
            self.conductor._find_task(task.id)[1])
        self.assertTrue(presence.resumable)

    # -- wedged dialogs -------------------------------------------------
    def test_boot_dialogs_are_answered(self) -> None:
        self.runtime.wedged = {"cond_task_x": "resume picker"}
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(self.runtime.keys_sent, ["cond_task_x"])
        self.assertEqual(report["dialogs_cleared"],
                         [{"session": "cond_task_x",
                           "cleared": "resume picker"}])

    def test_a_quiet_sweep_reports_nothing(self) -> None:
        task = self.a_running_task()
        self.runtime.live = {f"cond_task_{task.id}"}
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report, {"dialogs_cleared": [], "workers_gone": [],
                                  "waiting_on_you": [], "retired": [],
                                  "panes_closed": [], "gui_lease": [],
                                  "watched": [], "reconciled": []})

    # -- robustness ------------------------------------------------------
    def test_a_runtime_without_the_hooks_is_tolerated(self) -> None:
        """Not every runtime hosts a PTY. The sweep must no-op, not crash."""
        self.conductor.runtime = FakeCodingAgentRuntime()
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report, {"dialogs_cleared": [], "workers_gone": [],
                                  "waiting_on_you": [], "retired": [],
                                  "panes_closed": [], "gui_lease": [],
                                  "watched": [], "reconciled": []})

    def test_a_throwing_runtime_does_not_break_the_sweep(self) -> None:
        task = self.a_running_task()

        async def boom():
            raise RuntimeError("tmux is gone")
        self.runtime.unstick = boom
        self.runtime.live = set()
        report = self.go(self.conductor.sweep_stuck())
        self.assertEqual(report["workers_gone"], [task.id])   # still swept


if __name__ == "__main__":
    unittest.main()


class AnApprovalNobodyIsWatching(unittest.TestCase):
    """The case that has no other safety net.

    Approval prompts are noticed by _watch, a task per session that only
    exists for sessions this process started. Startup leaves workers
    asleep, so after a restart a backgrounded worker that hits a prompt has
    nobody reading its pane. It waits for a card that will never appear -
    invisible precisely because the thing that would have seen it is gone.
    """

    def setUp(self) -> None:
        from conductor.tmux_runtime import _prompt_in
        self.prompt_in = _prompt_in

    APPROVAL = ("⏺ I need to install a dependency.\n"
                "Do you want to proceed?\n"
                "  1. Yes\n  2. No, and tell Claude what to do differently\n")
    TRUST = ("Accessing workspace: /w\n"
             "Is this a project you created or one you trust?\n"
             "  1. Yes, I trust this folder\n  2. No, exit\n")
    WORKING = "⏺ Reading auth.py\n✻ Brewed for 3s\n❯ \n"

    def test_a_real_approval_is_reported(self) -> None:
        found = self.prompt_in(self.APPROVAL)
        self.assertIn("Do you want to proceed", found)

    def test_a_boot_dialog_is_not_an_approval(self) -> None:
        """unstick answers those. Reporting one would ask the user to
        approve something the system already handles for them."""
        self.assertEqual(self.prompt_in(self.TRUST), "")

    def test_a_working_pane_is_not_waiting(self) -> None:
        self.assertEqual(self.prompt_in(self.WORKING), "")

    def test_the_question_carries_its_context(self) -> None:
        """A bare "1. Yes" tells the user nothing about what they are
        agreeing to."""
        found = self.prompt_in(self.APPROVAL)
        self.assertIn("install a dependency", found)

    def test_a_watched_session_is_not_swept(self) -> None:
        """Its own watcher will raise it; two raisers means two cards."""
        from unittest import mock

        from conductor.tmux_runtime import TmuxClaudeRuntime
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        sess = mock.Mock(watcher=mock.Mock(done=lambda: False))
        sess.name = "cond_task_a"
        rt.sessions = {"s": sess}
        rt.live_session_names = lambda: {"cond_task_a"}
        rt._tmux = lambda *a: mock.Mock(stdout=self.APPROVAL, returncode=0)
        self.assertEqual(rt.unwatched_prompts(), [])

    def test_an_unwatched_session_is_swept(self) -> None:
        from unittest import mock

        from conductor.tmux_runtime import TmuxClaudeRuntime
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        rt.sessions = {}
        rt.live_session_names = lambda: {"cond_task_a"}
        rt._tmux = lambda *a: mock.Mock(stdout=self.APPROVAL, returncode=0)
        found = rt.unwatched_prompts()
        self.assertEqual(len(found), 1)
        self.assertIn("Do you want", found[0]["question"])


class AnsweringADialogWithoutAssumingItsShape(unittest.TestCase):
    """Pressing Enter answers whatever happens to be selected.

    That held in a tmux pane, where Claude Code's trust prompt offers
    "Yes, I trust this folder" first. It does not hold in a cmux surface,
    which puts "No, exit" first - verified by running one. The watchdog
    written to rescue stuck workers would have exited them instead.
    """

    def setUp(self) -> None:
        from conductor.tmux_runtime import choose_option
        self.choose = choose_option

    TMUX = ("Is this a project you trust?\n"
            "❯ 1. Yes, I trust this folder\n"
            "  2. No, exit\n"
            "Enter to confirm · Esc to cancel\n")
    CMUX = ("Is this a project you created or one you trust?\n"
            "❯ No, exit\n"
            "  Yes, I trust this folder\n"
            "Enter to confirm · Esc to cancel\n")

    def test_no_movement_when_the_wanted_option_is_selected(self):
        self.assertEqual(self.choose(self.TMUX, "trust this folder"), 0)

    def test_it_moves_when_the_dangerous_option_is_selected(self):
        self.assertEqual(self.choose(self.CMUX, "trust this folder"), 1)

    def test_an_absent_option_answers_nothing(self):
        """Better a worker that waits than one told to exit."""
        self.assertEqual(self.choose("no dialog here", "trust this folder"), -1)
        self.assertEqual(self.choose("", "trust this folder"), -1)

    def test_the_exit_option_is_never_what_we_land_on(self):
        for pane in (self.TMUX, self.CMUX):
            lines = [ln.strip() for ln in pane.splitlines() if ln.strip()]
            options = [ln for ln in lines
                       if ln.lstrip("❯>* ").lower().startswith(("1.", "2.",
                                                                "yes", "no"))]
            selected = next((i for i, ln in enumerate(options)
                             if ln.startswith("❯")), 0)
            landed = options[selected + self.choose(pane, "trust this folder")]
            self.assertIn("trust this folder", landed.lower())
            self.assertNotIn("no, exit", landed.lower())

    def test_a_reordered_dialog_is_still_answered_correctly(self):
        """The point is not two known layouts, it is not depending on the
        layout at all."""
        pane = ("Trust this folder?\n"
                "  Maybe later\n"
                "❯ No, exit\n"
                "  Yes, I trust this folder\n")
        self.assertEqual(self.choose(pane, "trust this folder"), 1)


class IdleRetirementTest(unittest.TestCase):
    """A task nobody addresses is closed by the sweep, not kept for ever.

    Measured before this existed: 29 tasks "waiting for you" after a day
    and a half, most from sessions whose windows were long gone. Nothing
    closed a task except someone saying so.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = base / "code"
        make_repo(self.roots / "posely")
        self.runtime = SweepableRuntime()
        self.events = []
        self.bus = ObservabilityBus()
        self.bus.subscribe(self.events.append)
        self.home = base / "home"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def conductor(self, idle_retire_s: float) -> GlobalConductor:
        return GlobalConductor(
            home=self.home, runtime=self.runtime, bus=self.bus,
            manager=FakeManagerBackend(), search_roots=[self.roots],
            workspace_factory=lambda project: FakeWorkspaceManager(),
            idle_retire_s=idle_retire_s)

    def go(self, coro):
        return asyncio.run(coro)

    def a_task_left(self, gc: GlobalConductor, status: str,
                    result: dict | None = None):
        pid = self.go(gc.handle_action(
            "register_project", {"path": str(self.roots / "posely")}))[
                "project_id"]
        task = self.go(gc.handle_action(
            "create_task", {"project_id": pid, "title": "Fix login",
                            "goal": "g"}))
        conductor, _ = gc._find_task(task.id)
        conductor.store.update(task.id, status=status, result=result)
        return task

    def status(self, gc, task_id: str) -> str:
        return gc._find_task(task_id)[1].status

    def test_an_answered_task_left_alone_is_completed(self) -> None:
        gc = self.conductor(idle_retire_s=0.01)
        task = self.a_task_left(gc, "waiting_for_user",
                                result={"summary": "done", "success": True})
        time.sleep(0.02)
        report = self.go(gc.sweep_stuck())
        self.assertEqual(report["retired"], [task.id])
        self.assertEqual(self.status(gc, task.id), "completed")
        retired = [e for e in self.events if e.type == "task.retired"]
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0].data["was"], "waiting_for_user")
        self.assertEqual(retired[0].data["closed_as"], "completed")

    def test_a_dead_worker_with_nothing_to_show_is_cancelled(self) -> None:
        gc = self.conductor(idle_retire_s=0.01)
        task = self.a_task_left(gc, "interrupted")
        time.sleep(0.02)
        self.go(gc.sweep_stuck())
        self.assertEqual(self.status(gc, task.id), "cancelled")
        self.assertEqual(
            [e.data["closed_as"] for e in self.events
             if e.type == "task.retired"], ["cancelled"])

    def test_a_recently_addressed_task_is_left_alone(self) -> None:
        gc = self.conductor(idle_retire_s=3600)
        task = self.a_task_left(gc, "waiting_for_user")
        report = self.go(gc.sweep_stuck())
        self.assertEqual(report["retired"], [])
        self.assertEqual(self.status(gc, task.id), "waiting_for_user")

    def test_off_by_default(self) -> None:
        gc = GlobalConductor(
            home=self.home, runtime=self.runtime, bus=self.bus,
            manager=FakeManagerBackend(), search_roots=[self.roots],
            workspace_factory=lambda project: FakeWorkspaceManager())
        task = self.a_task_left(gc, "waiting_for_user")
        time.sleep(0.02)
        report = self.go(gc.sweep_stuck())
        self.assertEqual(report["retired"], [])
        self.assertEqual(self.status(gc, task.id), "waiting_for_user")

    def test_only_the_resting_states_are_retired(self) -> None:
        """A worker mid-work is never closed for being quiet: silence while
        running is what tests look like."""
        gc = self.conductor(idle_retire_s=0.01)
        task = self.a_task_left(gc, "running")
        self.runtime.live.add(f"cond_task_{task.id}")
        time.sleep(0.02)
        self.go(gc.sweep_stuck())
        self.assertEqual(self.status(gc, task.id), "running")

    def test_retired_is_history_not_deletion(self) -> None:
        gc = self.conductor(idle_retire_s=0.01)
        task = self.a_task_left(gc, "waiting_for_user")
        time.sleep(0.02)
        self.go(gc.sweep_stuck())
        found = self.go(gc.handle_action("search_sessions",
                                         {"query": "login"}))
        self.assertIn(task.id, [row.get("task_id") for row in found])


class FinishedPanesStayReadable(IdleRetirementTest):
    """Completion leaves the worker's pane open; the sweep closes it once
    the task has sat unaddressed for the idle-retire window."""

    def test_a_fresh_completion_keeps_its_pane(self) -> None:
        gc = self.conductor(idle_retire_s=3600)
        task = self.a_task_left(gc, "waiting_for_user",
                                result={"summary": "done", "success": True})
        conductor, _ = gc._find_task(task.id)
        self.go(conductor.complete_task(task.id))
        report = self.go(gc.sweep_stuck())
        self.assertEqual(report["panes_closed"], [])
        self.assertNotIn(("destroy", task.provider_session_id), self.runtime.calls)

    def test_an_old_completion_has_its_pane_closed(self) -> None:
        gc = self.conductor(idle_retire_s=0.01)
        task = self.a_task_left(gc, "waiting_for_user",
                                result={"summary": "done", "success": True})
        conductor, _ = gc._find_task(task.id)
        self.go(conductor.complete_task(task.id))
        time.sleep(0.02)
        report = self.go(gc.sweep_stuck())
        self.assertEqual(report["panes_closed"], [task.id])
        self.assertIn(("destroy", task.provider_session_id), self.runtime.calls)
        self.assertIn("task.pane_closed", [e.type for e in self.events])
        # Idempotent: nothing left to close next time.
        self.assertEqual(self.go(gc.sweep_stuck())["panes_closed"], [])

    def test_an_idle_retirement_closes_the_pane_at_once(self) -> None:
        gc = self.conductor(idle_retire_s=0.01)
        task = self.a_task_left(gc, "waiting_for_user",
                                result={"summary": "done", "success": True})
        time.sleep(0.02)
        self.go(gc.sweep_stuck())
        self.assertEqual(self.status(gc, task.id), "completed")
        self.assertIn(("destroy", task.provider_session_id), self.runtime.calls)
