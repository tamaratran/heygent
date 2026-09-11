"""One conductor per home, and a GUI lease that lapses when it dies.

The night of 2026-09-08 read as two conductors fighting over one machine:
messages to a worker bouncing with `duplicate session`, replies spoken
over each other, and workers still driving the screen after a restart.
Two of them WOULD do all of that - one microphone (VoiceCoordinator
serialises within a process and cannot serialise across two), one set of
tmux session names, one set of workers - and nothing stopped it or could
tell you whether it had happened. It had not, as it turned out; `ps`
lists `uv run ... conduct.py` beside the python it spawned, and one
conductor reads as two. The rest was real: a worker left behind by the
restart was still driving Chrome with nothing supervising it.

The lock answers both. A second conductor refuses to start rather than
being something to diagnose afterwards, and the same file is the roster
of workers still allowed to touch the keyboard - so letting go of it
takes the keyboard back.

Run with:  python3 -m unittest tests.test_one_conductor -v
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import instance


def fake_ps(table: dict):
    """A `ps -p PID -o lstart=` that answers from a dict of pid -> start."""
    def run(argv, **kwargs):
        pid = int(argv[argv.index("-p") + 1])
        started = table.get(pid)
        return mock.Mock(returncode=0 if started else 1,
                         stdout=(started or ""), stderr="")
    return run


class TheLockRefusesASecond(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.mine = os.getpid()

    def test_the_first_conductor_gets_the_home(self) -> None:
        held = instance.acquire(self.home, "run_1",
                                run=fake_ps({self.mine: "Mon Sep 8 10:00"}))
        self.assertEqual(held.pid, self.mine)
        self.assertEqual(instance.read(self.home).run_id, "run_1")

    def test_a_second_conductor_is_refused_by_name(self) -> None:
        """The failure the user should see is 'one is already running',
        not twenty minutes later 'duplicate session'."""
        other = self.mine + 1
        instance._write(instance.lock_path(self.home),
                        instance.Instance(pid=other, run_id="run_1",
                                          started_at="Mon Sep 8 10:00"))
        with self.assertRaises(instance.AlreadyRunning) as caught:
            instance.acquire(self.home, "run_2",
                             run=fake_ps({other: "Mon Sep 8 10:00",
                                          self.mine: "Mon Sep 8 11:00"}))
        self.assertEqual(caught.exception.instance.pid, other)
        self.assertIn("already running", str(caught.exception))

    def test_a_lock_left_by_a_dead_conductor_does_not_block(self) -> None:
        instance._write(instance.lock_path(self.home),
                        instance.Instance(pid=self.mine + 2, run_id="old",
                                          started_at="Mon Sep 8 10:00"))
        held = instance.acquire(self.home, "run_2",
                                run=fake_ps({self.mine: "Mon Sep 8 11:00"}))
        self.assertEqual(held.pid, self.mine)

    def test_a_recycled_pid_is_not_the_conductor(self) -> None:
        """The pid of a conductor that died in the night is exactly the
        kind of number a fresh process gets, so the lock records what ps
        says the process started at and compares that too."""
        other = self.mine + 3
        instance._write(instance.lock_path(self.home),
                        instance.Instance(pid=other, run_id="old",
                                          started_at="Mon Sep 8 10:00"))
        held = instance.acquire(
            self.home, "run_2",
            run=fake_ps({other: "Tue Sep 9 09:00",     # somebody else now
                         self.mine: "Tue Sep 9 09:30"}))
        self.assertEqual(held.pid, self.mine)

    def test_takeover_asks_the_live_one_to_go_first(self) -> None:
        other = self.mine + 4
        instance._write(instance.lock_path(self.home),
                        instance.Instance(pid=other, run_id="run_1",
                                          started_at="Mon Sep 8 10:00"))
        living = {other: "Mon Sep 8 10:00", self.mine: "Mon Sep 8 11:00"}
        signalled = []

        def kill(pid, sig):
            signalled.append((pid, sig))
            living.pop(pid, None)          # it exits, as a conductor does

        with mock.patch.object(instance.os, "kill", kill):
            held = instance.acquire(self.home, "run_2", takeover=True,
                                    run=fake_ps(living), sleep=lambda s: None)
        self.assertEqual([pid for pid, _ in signalled], [other])
        self.assertEqual(held.pid, self.mine)

    def test_takeover_gives_up_rather_than_running_beside_it(self) -> None:
        other = self.mine + 5
        instance._write(instance.lock_path(self.home),
                        instance.Instance(pid=other, run_id="run_1",
                                          started_at="Mon Sep 8 10:00"))
        living = {other: "Mon Sep 8 10:00", self.mine: "Mon Sep 8 11:00"}
        with mock.patch.object(instance.os, "kill", lambda *a: None):
            with self.assertRaises(instance.AlreadyRunning) as caught:
                instance.acquire(self.home, "run_2", takeover=True,
                                 run=fake_ps(living), wait_s=0.05,
                                 sleep=lambda s: None)
        self.assertIn("did not exit", str(caught.exception))

    def test_releasing_is_only_ever_our_own(self) -> None:
        instance.acquire(self.home, "run_1",
                         run=fake_ps({self.mine: "Mon Sep 8 10:00"}))
        instance._write(instance.lock_path(self.home),
                        instance.Instance(pid=self.mine + 6, run_id="theirs",
                                          started_at="x"))
        self.assertFalse(instance.release(self.home, "run_1"))
        self.assertIsNotNone(instance.read(self.home))

    def test_a_heartbeat_never_steals_the_lock_back(self) -> None:
        instance._write(instance.lock_path(self.home),
                        instance.Instance(pid=self.mine + 7, run_id="theirs",
                                          started_at="x"))
        self.assertFalse(instance.heartbeat(self.home, "ours", ["task_a"]))
        self.assertEqual(instance.read(self.home).run_id, "theirs")

    def test_a_torn_lock_file_is_not_a_conductor(self) -> None:
        instance.lock_path(self.home).parent.mkdir(parents=True, exist_ok=True)
        instance.lock_path(self.home).write_text("{not json")
        self.assertIsNone(instance.read(self.home))


class TheKeyboardGoesBackWhenTheConductorDies(unittest.TestCase):
    """The GUI lease. A worker may drive the screen only while the
    conductor that supervises it is the live one and still counts it."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.mine = os.getpid()
        # The real ps here: the lease asks the process table about a pid
        # that is genuinely running - this one - which is the whole point
        # of the check it makes.
        instance.acquire(self.home, "run_1")
        instance.heartbeat(self.home, "run_1", ["task_a"])

    def env(self, task_id: str) -> dict:
        return {instance.TASK_ENV: task_id, instance.HOME_ENV: str(self.home)}

    def test_a_supervised_worker_may_act(self) -> None:
        self.assertTrue(instance.lease(None, environ=self.env("task_a")))

    def test_a_worker_the_conductor_does_not_count_may_not(self) -> None:
        """A restart does not hand the keyboard back. Five workers
        survived one; one of them was still driving Chrome."""
        held = instance.lease(None, environ=self.env("task_b"))
        self.assertFalse(held)
        self.assertIn("does not count task_b", held.reason)

    def test_no_conductor_at_all_means_no_keyboard(self) -> None:
        instance.release(self.home, "run_1")
        held = instance.lease(None, environ=self.env("task_a"))
        self.assertFalse(held)
        self.assertIn("no conductor is running", held.reason)

    def test_a_conductor_that_was_killed_means_no_keyboard(self) -> None:
        """SIGKILL leaves the file behind; the process table does not."""
        raw = json.loads(instance.lock_path(self.home).read_text())
        raw["pid"] = self.mine + 9
        raw["started_at"] = "a long time ago"
        instance.lock_path(self.home).write_text(json.dumps(raw))
        held = instance.lease(None, environ=self.env("task_a"))
        self.assertFalse(held)
        self.assertIn("no longer running", held.reason)

    def test_a_wedged_conductor_stops_renewing_the_lease(self) -> None:
        held = instance.lease(
            None, environ=self.env("task_a"),
            now=lambda: __import__("time").time() + instance.LEASE_STALE_S + 60)
        self.assertFalse(held)
        self.assertIn("has not checked in", held.reason)

    def test_a_person_at_a_shell_is_not_gated(self) -> None:
        """The driver is also a command someone runs by hand, and the
        computer-use smoke test drives TextEdit with no conductor at all.
        The gate is for a process that says which worker it is."""
        self.assertTrue(instance.lease(None, environ={}))


class HangingUpIsNotARequestToShutDown(unittest.TestCase):
    """The conductor is started detached from a worker's shell:

        nohup ./conduct.sh > ~/.voice-conductor/logs/conduct-console.log 2>&1 &

    and nohup works by setting SIGHUP to SIG_IGN, which is what keeps the
    conductor alive when the window it was launched from closes.
    asyncio's add_signal_handler REPLACES that ignore, so handling SIGHUP
    - which the takeover work briefly did - would let a closing worker
    window take the user's voice session with it, and leave a clean log
    behind saying nothing about why.

    SIGTERM is different: it is a request, and it is how --takeover asks
    the live conductor to leave.
    """

    def test_only_sigterm_is_handled(self) -> None:
        import inspect
        import conduct
        source = inspect.getsource(conduct.main)
        self.assertIn("add_signal_handler(signal.SIGTERM", source)
        # The comment above it says SIGHUP; the code must not.
        code = [line for line in source.splitlines()
                if "add_signal_handler" in line
                and not line.lstrip().startswith("#")]
        self.assertEqual(len(code), 1, code)
        self.assertNotIn("SIGHUP", "".join(code))


class TheRosterIsWhatTheConductorIsActuallySupervising(unittest.TestCase):
    """The lease is only as good as what fills it: computer-use tasks
    this conductor holds, and nothing else."""

    def setUp(self) -> None:
        from tests.test_global import (FakeManagerBackend,
                                       FakeWorkspaceManager, make_repo)
        from tests.test_watchdog import SweepableRuntime
        from conductor.global_conductor import GlobalConductor
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        roots = base / "code"
        make_repo(roots / "posely")
        self.home = base / "home"
        self.gc = GlobalConductor(
            home=self.home, runtime=SweepableRuntime(),
            manager=FakeManagerBackend(), search_roots=[roots],
            workspace_factory=lambda project: FakeWorkspaceManager())
        self.project = self.gc.register_project(str(roots / "posely"))["project_id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def a_task(self, computer: bool, status: str):
        task = asyncio.run(self.gc.create_task(
            "t", "g", project_id=self.project, computer=computer))
        conductor, _ = self.gc._find_task(task.id)
        return conductor.store.update(task.id, status=status)

    def test_only_computer_use_tasks_get_a_lease(self) -> None:
        plain = self.a_task(computer=False, status="running")
        driving = self.a_task(computer=True, status="running")
        roster = self.gc.gui_tasks()
        self.assertIn(driving.id, roster)
        self.assertNotIn(plain.id, roster)

    def test_a_finished_worker_stops_holding_the_screen(self) -> None:
        done = self.a_task(computer=True, status="completed")
        self.assertNotIn(done.id, self.gc.gui_tasks())

    def test_an_interrupted_worker_stops_holding_the_screen(self) -> None:
        """This is the state a restart leaves them in, and the state one
        of them was in while it went on driving Chrome."""
        stale = self.a_task(computer=True, status="interrupted")
        self.assertNotIn(stale.id, self.gc.gui_tasks())

    def test_a_worker_waiting_on_the_user_still_holds_it(self) -> None:
        """Mid-action, with a window under its cursor and a question on
        screen. Taking the keyboard back there would strand it."""
        asking = self.a_task(computer=True, status="waiting_for_user")
        self.assertIn(asking.id, self.gc.gui_tasks())

    def test_publishing_writes_the_roster_where_a_worker_reads_it(self) -> None:
        instance.acquire(self.home, "run_1")
        driving = self.a_task(computer=True, status="running")
        self.gc.publish_gui_lease()
        held = instance.lease(None, environ={instance.TASK_ENV: driving.id,
                                             instance.HOME_ENV: str(self.home)})
        self.assertTrue(held, held.reason)


if __name__ == "__main__":
    unittest.main()
