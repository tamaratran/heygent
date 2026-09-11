"""A watcher does not declare its session ended on one missed poll.

Measured 2026-08-29, twice in one run (15:16:58Z and 22:30:55Z): every
watched session - the workers and the Boss - was marked "tmux session
ended" in the same second, while every process was alive. Under cmux,
has-session is a workspace listing, and a listing that fails is cached
as "nobody" for a second; the watcher quit on that one answer, the tasks
went terminal, the workers' reports were dropped, and the Boss's replies
were never read again.

Now the session has to be missing on several consecutive polls that
together outlast the listing cache, a growing transcript is proof of
life, a watcher says why it stopped, and a failed listing answers with
the last good one.

Run with:  python3 -m unittest tests.test_watcher_survives_a_miss -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor.cmux_runtime import MANAGED_MARK, CmuxClaudeRuntime, _Result
from conductor.tmux_runtime import TmuxClaudeRuntime, _TmuxSession


class Base(unittest.TestCase):
    def runtime(self, answers) -> TmuxClaudeRuntime:
        """A runtime whose has-session answers come from `answers`; the
        last answer repeats."""
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        rt.sessions, rt.transcript, rt.epoch = {}, None, "ep_test"
        rt.WATCH_POLL_S = 0.01
        rt.WATCH_GONE_MISSES = 3
        rt.WATCH_GONE_AFTER_S = 0.05
        answers = list(answers)
        self.polls = 0

        def alive(name):
            self.polls += 1
            return answers.pop(0) if len(answers) > 1 else answers[0]
        rt._alive = alive
        rt._process_alive = lambda sess: False    # no process vouches
        rt._pane = mock.AsyncMock(return_value="")
        rt._check_approval_prompt = lambda sess, pane=None: None
        return rt

    def session(self, jsonl_path=None) -> _TmuxSession:
        sess = _TmuxSession(task_id="task_a", name="cond_task_a",
                            working_directory="/tmp", jsonl_path=jsonl_path)
        self.events = []
        sess.handlers.append(self.events.append)
        return sess

    def watch_for(self, rt, sess, seconds):
        async def run():
            task = asyncio.create_task(rt._watch(sess))
            try:
                await asyncio.wait_for(asyncio.shield(task), seconds)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            return task
        with mock.patch("conductor.tmux_runtime.application_log") as log:
            asyncio.run(run())
        return log

    def failures(self):
        return [e for e in self.events if e.type == "failed"]


class OneMissIsNotTheEnd(Base):
    def test_a_single_no_is_forgiven(self):
        rt = self.runtime([False, True])
        sess = self.session()
        self.watch_for(rt, sess, 0.2)
        self.assertEqual(self.failures(), [])
        self.assertEqual(sess.status, "running")
        self.assertGreater(self.polls, 5)

    def test_a_streak_shorter_than_the_cache_is_forgiven(self):
        """Three quick misses inside one cached bad listing, then cmux
        answers again."""
        rt = self.runtime([False, False, True])
        rt.WATCH_GONE_AFTER_S = 0.5           # the cache outlasts the polls
        sess = self.session()
        self.watch_for(rt, sess, 0.2)
        self.assertEqual(self.failures(), [])

    def test_the_miss_and_the_recovery_are_logged(self):
        rt = self.runtime([False, True])
        sess = self.session()
        log = self.watch_for(rt, sess, 0.2)
        events = [c.args[1] for c in log.call_args_list]
        self.assertIn("runtime.session_missing", events)
        self.assertIn("runtime.session_found", events)


class AStreakIsTheEnd(Base):
    def test_a_session_missing_for_long_enough_has_ended(self):
        rt = self.runtime([False])
        sess = self.session()
        log = self.watch_for(rt, sess, 1.0)
        self.assertEqual(len(self.failures()), 1)
        self.assertEqual(self.failures()[0].error, "tmux session ended")
        self.assertEqual(sess.status, "disconnected")
        self.assertGreaterEqual(self.polls, rt.WATCH_GONE_MISSES)
        ended = [c for c in log.call_args_list
                 if c.args[1] == "runtime.watch_ended"]
        self.assertEqual(len(ended), 1)
        self.assertIn("missed polls", ended[0].args[2])

    def test_a_growing_transcript_is_proof_of_life(self):
        """The listing says nobody; the session keeps writing. Believe
        the session."""
        rt = self.runtime([False])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sess.jsonl"
            path.write_text("")
            sess = self.session(jsonl_path=path)

            async def keep_writing():
                for _ in range(30):
                    with path.open("a") as fh:
                        fh.write(json.dumps({"type": "system"}) + "\n")
                    await asyncio.sleep(0.01)

            async def run():
                writer = asyncio.create_task(keep_writing())
                watcher = asyncio.create_task(rt._watch(sess))
                await writer
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass
            with mock.patch("conductor.tmux_runtime.application_log"):
                asyncio.run(run())
        self.assertEqual(self.failures(), [])
        self.assertGreater(self.polls, rt.WATCH_GONE_MISSES * 2)

    def test_a_watcher_that_crashes_keeps_watching(self):
        """A pane read that raises is reported, once, and the watcher
        goes on - a session is not ended by our own exception."""
        rt = self.runtime([True])
        calls = {"n": 0}

        def flaky(name):
            calls["n"] += 1
            if calls["n"] <= 3:                  # one streak, then fine
                raise OSError("could not ask")
            return True
        rt._alive = flaky
        sess = self.session()
        log = self.watch_for(rt, sess, 0.15)
        self.assertEqual(self.failures(), [])
        self.assertGreater(calls["n"], 5)
        events = [c.args[1] for c in log.call_args_list]
        self.assertEqual(events.count("runtime.watch_failed"), 1)
        self.assertIn("runtime.watch_recovered", events)


class TheProcessTableHasTheLastWord(Base):
    """The host can say "nobody" for as long as it likes: while the
    session's claude process is running, the session is not ended."""

    def test_a_running_process_keeps_a_session_the_host_lost(self):
        rt = self.runtime([False])
        asked = []
        rt._process_alive = lambda sess: asked.append(sess.name) or True
        sess = self.session()
        log = self.watch_for(rt, sess, 0.4)
        self.assertEqual(self.failures(), [])
        self.assertEqual(sess.status, "running")
        events = [c.args[1] for c in log.call_args_list]
        self.assertEqual(events.count("runtime.session_process_alive"), 1)
        # Asked once per threshold, not on every poll.
        self.assertGreaterEqual(len(asked), 1)
        self.assertLess(len(asked), self.polls // 2)

    def test_a_session_whose_process_is_gone_has_ended(self):
        rt = self.runtime([False])
        rt._process_alive = lambda sess: False
        sess = self.session()
        self.watch_for(rt, sess, 1.0)
        self.assertEqual(len(self.failures()), 1)

    def test_a_process_that_dies_later_ends_the_session_then(self):
        rt = self.runtime([False])
        answers = [True, True, False]
        rt._process_alive = lambda sess: answers.pop(0) if len(answers) > 1 \
            else answers[0]
        sess = self.session()
        self.watch_for(rt, sess, 1.5)
        self.assertEqual(len(self.failures()), 1)
        self.assertEqual(sess.status, "disconnected")


class AskingTheProcessTable(unittest.TestCase):
    """_process_alive itself, with the process table faked."""

    def runtime(self):
        return TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)

    def session(self, session_id, cwd="/work/task_a"):
        return _TmuxSession(task_id="task_a", name="cond_task_a",
                            working_directory=cwd, session_id=session_id)

    def fake_run(self, by_id="", pids="", cwds=""):
        def run(argv, **kw):
            done = mock.Mock(returncode=0, stdout="", stderr="")
            if argv[0] == "pgrep" and argv[-1] != "claude":
                done.stdout = by_id
            elif argv[0] == "pgrep":
                done.stdout = pids
            elif argv[0] == "lsof":
                done.stdout = cwds
            return done
        return run

    def test_a_session_on_a_command_line_is_found_by_its_id(self):
        with mock.patch("conductor.tmux_runtime.subprocess.run",
                        self.fake_run(by_id="4242\n")):
            self.assertTrue(self.runtime()._process_alive(
                self.session("11111111-2222")))

    def test_a_fresh_worker_is_found_by_its_checkout(self):
        cwds = "p4242\nfcwd\nn/work/task_a\np4343\nfcwd\nn/work/task_b\n"
        with mock.patch("conductor.tmux_runtime.subprocess.run",
                        self.fake_run(pids="4242\n4343\n", cwds=cwds)):
            rt = self.runtime()
            self.assertTrue(rt._process_alive(self.session(None)))
            self.assertTrue(rt._process_alive(
                self.session(None, cwd="/work/task_a/")))
            self.assertFalse(rt._process_alive(
                self.session(None, cwd="/work/task_c")))

    def test_the_sweep_can_ask_about_a_window_it_was_shown(self):
        """worker_process_alive: the sweep's question about a listed
        window, answered from the process table by checkout or by id -
        with no session object, which a restart has emptied."""
        cwds = "p4242\nfcwd\nn/work/task_a\n"
        with mock.patch("conductor.tmux_runtime.subprocess.run",
                        self.fake_run(pids="4242\n", cwds=cwds)):
            rt = self.runtime()
            rt.sessions = {}
            self.assertTrue(rt.worker_process_alive(
                "cond_task_a", "/work/task_a", None))
            self.assertFalse(rt.worker_process_alive(
                "cond_task_b", "/work/task_b", None),
                "an empty window read as a worker")
            # Nothing to look for is no opinion, and no opinion is alive.
            self.assertTrue(rt.worker_process_alive("cond_task_b", None, None))
        with mock.patch("conductor.tmux_runtime.subprocess.run",
                        self.fake_run(by_id="4242\n")):
            rt = self.runtime()
            rt.sessions = {}
            self.assertTrue(rt.worker_process_alive(
                "cond_task_b", "/work/task_b", "11111111-2222"))

    def test_the_asker_s_own_ancestors_are_not_left_out(self):
        """macOS pgrep omits its ancestors by default. Asked from inside
        a worker, the plain form listed every claude but that one."""
        import sys
        from conductor.tmux_runtime import PGREP
        seen = []

        def run(argv, **kw):
            seen.append(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("conductor.tmux_runtime.subprocess.run", run):
            self.runtime()._process_alive(self.session(None))
        pgreps = [a for a in seen if a[0] == "pgrep"]
        self.assertTrue(pgreps)
        for argv in pgreps:
            self.assertEqual(argv[:len(PGREP)], PGREP)
            self.assertEqual("-a" in argv, sys.platform == "darwin")

    def test_no_claude_at_all_is_no_process(self):
        with mock.patch("conductor.tmux_runtime.subprocess.run",
                        self.fake_run()):
            self.assertFalse(self.runtime()._process_alive(
                self.session("11111111-2222")))

    def test_not_being_able_to_ask_reads_as_alive(self):
        def broken(argv, **kw):
            raise OSError("no pgrep here")
        with mock.patch("conductor.tmux_runtime.subprocess.run", broken):
            self.assertTrue(self.runtime()._process_alive(
                self.session("11111111-2222")))


class AWatcherThatEndedIsStartedAgain(Base):
    def test_rewatch_restarts_a_finished_watcher(self):
        rt = self.runtime([True])
        sess = self.session()
        rt.sessions["sid"] = sess

        async def run():
            sess.watcher = asyncio.create_task(asyncio.sleep(0))
            await sess.watcher                       # ended, like the real one
            with mock.patch("conductor.tmux_runtime.application_log") as log:
                self.assertTrue(rt.rewatch("sid"))
                self.assertFalse(rt.rewatch("sid"))  # already reading
            self.assertIn("runtime.watch_restarted",
                          [c.args[1] for c in log.call_args_list])
            await asyncio.sleep(0.05)
            self.assertFalse(sess.watcher.done())
            sess.watcher.cancel()
            try:
                await sess.watcher
            except asyncio.CancelledError:
                pass
        asyncio.run(run())
        self.assertGreater(self.polls, 1)

    def test_rewatch_knows_nothing_about_a_strange_session(self):
        rt = self.runtime([True])
        self.assertFalse(rt.rewatch("nobody"))

    def test_the_restarted_watcher_delivers_what_was_written_meanwhile(self):
        """The reply the Boss wrote while nobody was reading is the one
        the user is waiting for."""
        rt = self.runtime([True])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sess.jsonl"
            path.write_text("")
            sess = self.session(jsonl_path=path)
            rt.sessions["sid"] = sess
            entry = {"type": "assistant", "uuid": "u1",
                     "message": {"role": "assistant", "stop_reason": "end_turn",
                                 "content": [{"type": "text",
                                              "text": "here is the answer"}]}}
            with path.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")

            async def run():
                sess.watcher = asyncio.create_task(asyncio.sleep(0))
                await sess.watcher
                with mock.patch("conductor.tmux_runtime.application_log"):
                    rt.rewatch("sid")
                await asyncio.sleep(0.1)
                sess.watcher.cancel()
                try:
                    await sess.watcher
                except asyncio.CancelledError:
                    pass
            asyncio.run(run())
        self.assertIn("completed", [e.type for e in self.events])


def listing(*workspaces) -> str:
    return json.dumps({"workspaces": list(workspaces)})


def ours(title="cond_task_a", uuid="WS-UUID"):
    return {"id": uuid, "custom_title": title, "ref": "workspace:2",
            "description": f"{MANAGED_MARK}: {title}",
            "current_directory": "/w"}


class AFailedListingIsNotAnEmptyOne(unittest.TestCase):
    def runtime(self):
        rt = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
        rt.cmux, rt._password, rt.places, rt.transcript = "/fake", "x", {}, None
        rt.WORKSPACES_TTL_S = 0.0
        self.failing = {"list": False, "json": False}
        self.listings = 0

        def cmux(*args):
            if args[:3] == ("workspace", "list", "--json"):
                self.listings += 1
                if self.failing["list"]:
                    return _Result(1, "", "cmux: connection refused")
                if self.failing["json"]:
                    return _Result(0, "cmux: warming up", "")
                return _Result(0, listing(ours()))
            if args[0] == "list-pane-surfaces":
                return _Result(0, "* surface:2 SF-UUID  cond_task_a\n")
            return _Result(0, "OK")
        rt._cmux = cmux
        return rt

    def test_a_failed_listing_answers_with_the_last_good_one(self):
        rt = self.runtime()
        with mock.patch("conductor.cmux_runtime.application_log") as log:
            self.assertEqual(rt._tmux("has-session", "-t", "cond_task_a")
                             .returncode, 0)
            self.failing["list"] = True
            self.assertEqual(rt._tmux("has-session", "-t", "cond_task_a")
                             .returncode, 0)
            self.failing["list"], self.failing["json"] = False, True
            self.assertEqual(rt._tmux("has-session", "-t", "cond_task_a")
                             .returncode, 0)
        events = [c.args[1] for c in log.call_args_list]
        self.assertEqual(events.count("cmux.listing_failed"), 1)

    def test_the_failure_and_the_recovery_are_logged_once_each(self):
        rt = self.runtime()
        with mock.patch("conductor.cmux_runtime.application_log") as log:
            rt._workspaces()
            self.failing["list"] = True
            rt._workspaces()
            rt._workspaces()
            self.failing["list"] = False
            rt._workspaces()
            rt._workspaces()
        events = [c.args[1] for c in log.call_args_list]
        self.assertEqual(events, ["cmux.listing_failed",
                                  "cmux.listing_recovered"])

    def test_with_nothing_good_yet_a_failure_still_reads_as_nobody(self):
        """Before any listing has succeeded there is nothing better to
        answer - but the failure is logged, which it never was."""
        rt = self.runtime()
        self.failing["list"] = True
        with mock.patch("conductor.cmux_runtime.application_log") as log:
            self.assertEqual(rt._tmux("has-session", "-t", "cond_task_a")
                             .returncode, 1)
        self.assertEqual([c.args[1] for c in log.call_args_list],
                         ["cmux.listing_failed"])

    def test_a_listing_failing_for_too_long_answers_nobody(self):
        """cmux that has not answered for LISTING_STALE_MAX_S is gone,
        not stumbling: the last good listing stops standing in for it."""
        rt = self.runtime()
        rt.LISTING_STALE_MAX_S = 30.0
        clock = {"t": 1000.0}
        with mock.patch("conductor.cmux_runtime.time.monotonic",
                        lambda: clock["t"]), \
                mock.patch("conductor.cmux_runtime.application_log") as log:
            self.assertEqual(rt._workspaces(), [ours()])
            self.failing["list"] = True
            clock["t"] += 5
            self.assertEqual(rt._workspaces(), [ours()])
            clock["t"] += 20
            self.assertEqual(rt._workspaces(), [ours()])
            clock["t"] += 10                     # 35 s without an answer
            self.assertEqual(rt._workspaces(), [])
            self.assertEqual(rt._tmux("has-session", "-t", "cond_task_a")
                             .returncode, 1)
            self.failing["list"] = False
            self.assertEqual(rt._workspaces(), [ours()])
        events = [c.args[1] for c in log.call_args_list]
        self.assertEqual(events, ["cmux.listing_failed", "cmux.listing_stale",
                                  "cmux.listing_recovered"])

    def test_a_workspace_we_closed_is_gone_even_if_the_listing_fails(self):
        """The last good listing is a fallback for cmux going quiet, not
        a way to keep a closed workspace alive: kill-session forgets it."""
        rt = self.runtime()
        rt._workspaces()
        rt._tmux("kill-session", "-t", "cond_task_a")
        self.assertNotIn("cond_task_a", rt.places)


if __name__ == "__main__":
    unittest.main()
