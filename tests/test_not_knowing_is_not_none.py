"""A worker we cannot ask about is not a worker that died.

Seen live: two tasks were created and declared gone within a minute -
task_7fbead35 after 28 seconds, task_e0a83a12 after 56 - while both
workspaces were sitting in cmux the whole time, healthy. Their cards went
stale, which is what the user saw as "the working card doesn't take me
anywhere".

The sweep marks every running task missing from live_session_names() as
gone. That listing swallowed its own failure and returned an empty set,
so one unreachable moment - cmux closed, mid-repair, password rewritten -
read as "every worker died".

Run with:  python3 -m unittest tests.test_not_knowing_is_not_none -v
"""

from __future__ import annotations

import unittest
import json
from unittest import mock

from conductor.cmux_runtime import CmuxClaudeRuntime, _Result
from conductor.tmux_runtime import TmuxClaudeRuntime


def runtime(returncode=0, stdout="", stderr=""):
    rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
    rt._tmux = lambda *a: mock.Mock(returncode=returncode, stdout=stdout,
                                    stderr=stderr)
    return rt


class AskingWhoIsAlive(unittest.TestCase):
    def test_a_listing_that_worked_is_believed(self):
        rt = runtime(stdout="cond_task_a\ncond_task_b\n")
        self.assertEqual(rt.live_session_names(),
                         {"cond_task_a", "cond_task_b"})

    def test_a_failed_listing_is_not_an_empty_one(self):
        """The difference between "nobody is alive" and "I could not ask".
        Answering the first when you mean the second kills every card."""
        rt = runtime(returncode=1, stderr="Connection refused, errno 61")
        self.assertIsNone(rt.live_session_names())

    def test_genuinely_no_sessions_is_still_an_answer(self):
        """tmux says this when there are really none, and the sweep should
        act on it."""
        rt = runtime(returncode=1, stderr="no server running on /tmp/tmux")
        self.assertEqual(rt.live_session_names(), set())


class TheCmuxSeamKeepsTheFailure(unittest.TestCase):
    def cmux_runtime(self, result):
        rt = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
        rt.cmux = "/fake/cmux"
        rt._password = "x"
        rt.places = {}
        rt.transcript = None
        rt._cmux = lambda *a, **k: result
        return rt

    def test_an_unreachable_cmux_is_reported_not_flattened(self):
        """It returned success with an empty list, so every worker looked
        dead the moment cmux stopped answering."""
        rt = self.cmux_runtime(_Result(1, "", "Connection refused"))
        out = rt._tmux("list-sessions", "-F", "#{session_name}")
        self.assertNotEqual(out.returncode, 0)

    def test_a_working_cmux_still_lists_its_workspaces(self):
        rt = self.cmux_runtime(_Result(0, json.dumps({"workspaces": [
            {"id": "UUID", "custom_title": "cond_task_a"},
            {"id": "UUID2", "custom_title": None}]})))
        out = rt._tmux("list-sessions", "-F", "#{session_name}")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.split(), ["cond_task_a"])

    def test_names_come_from_the_json_not_the_text_listing(self):
        """cmux 0.64.22 prints `workspace:12  cond_task_x` - two columns
        where the text parser expected three - so every title read as
        empty and the sweep buried every worker 30 s after it started.
        Measured on 2026-08-28: has-session said alive, the listing said
        nobody. The JSON listing is what send and focus already trust."""
        real_text = ("  workspace:12  cond_task_a\n"
                     "* workspace:18  cond_task_b  [selected]\n"
                     "  workspace:10  ~\n")
        rt = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
        rt.cmux, rt._password, rt.places, rt.transcript = "/f", "x", {}, None
        rt._cmux = lambda *a, **k: (
            _Result(0, json.dumps({"workspaces": [
                {"id": "U1", "custom_title": "cond_task_a"},
                {"id": "U2", "custom_title": "cond_task_b"},
                {"id": "U3", "custom_title": None}]}))
            if a[:3] == ("workspace", "list", "--json")
            else _Result(0, real_text))
        self.assertEqual(rt.live_session_names(),
                         {"cond_task_a", "cond_task_b"})

    def test_a_listing_that_is_not_json_is_a_failure_not_an_empty_list(self):
        rt = self.cmux_runtime(_Result(0, "  workspace:12  cond_task_a\n"))
        self.assertIsNone(rt.live_session_names())

    def test_the_second_opinion_asks_by_name(self):
        """What the sweep consults before burying a worker the listing
        did not name: has-session, through the same lookup send uses."""
        rt = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
        rt.cmux, rt._password, rt.transcript = "/f", "x", None
        rt.places = {"cond_task_a": ("U1", "S1")}
        rt._cmux = lambda *a, **k: _Result(0, json.dumps({"workspaces": [
            {"id": "U1", "custom_title": "cond_task_a",
             "description": "conductor-managed agent session: cond_task_a"}]}))
        self.assertTrue(rt.session_alive("cond_task_a"))
        self.assertFalse(rt.session_alive("cond_task_zzz"))


class TheSweepDoesNotActOnIgnorance(unittest.TestCase):
    def test_no_answer_means_no_workers_are_buried(self):
        """The guard already existed - the sweep skips when the listing is
        None - but nothing ever returned None."""
        import inspect

        from conductor.global_conductor import GlobalConductor
        source = inspect.getsource(GlobalConductor._sweep_wedged_workers) \
            if hasattr(GlobalConductor, "_sweep_wedged_workers") else ""
        if not source:
            for name, member in inspect.getmembers(GlobalConductor):
                if inspect.isfunction(member):
                    text = inspect.getsource(member)
                    if "live_session_names" in text:
                        source = text
                        break
        self.assertIn("is not None", source,
                      "the sweep acts on a listing it could not get")


if __name__ == "__main__":
    unittest.main()
