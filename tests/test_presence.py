"""Open means what the user has in front of them.

The bug: a provider session that still exists counted as open. Every
finished turn leaves a task in waiting_for_user, so the roster grew forever
and the Manager answered "how many agents do I have open?" from the
filesystem rather than from the screen.

Run with:  python3 -m unittest tests.test_presence -v
"""

from __future__ import annotations

import unittest

from conductor.presence import (CURRENT, HIDDEN, RECENT, RECENT_RETENTION_S,
                                RETIRED, VISIBLE_ACTIVE, VISIBLE_RECENT,
                                Presence, classify, is_terminal, roster)


class OpennessTest(unittest.TestCase):
    def test_a_resumable_finished_session_is_not_open(self) -> None:
        p = classify("completed", seconds_since_finished=RECENT_RETENTION_S + 1,
                     has_provider_session=True)
        self.assertFalse(p.is_open)
        self.assertTrue(p.resumable)
        self.assertEqual(p.display, HIDDEN)
        self.assertEqual(p.lifecycle, RETIRED)

    def test_a_provider_session_existing_never_implies_open(self) -> None:
        for execution in ("completed", "failed", "cancelled"):
            p = classify(execution, has_provider_session=True)
            self.assertFalse(p.is_open, execution)

    def test_work_in_flight_is_open(self) -> None:
        for execution in ("starting", "running", "waiting_for_approval",
                          "waiting_for_input", "paused", "interrupted"):
            self.assertTrue(classify(execution).is_open, execution)

    def test_waiting_for_user_is_open_while_visible(self) -> None:
        """It is the state a worker rests in between instructions, so it is
        open while on screen - but only because it is on screen."""
        self.assertTrue(classify("waiting_for_user").is_open)
        self.assertFalse(classify("waiting_for_user",
                                  hidden_by_user=True).is_open)


class RecentTest(unittest.TestCase):
    def test_a_fresh_completion_stays_visible_but_is_not_open(self) -> None:
        p = classify("completed", seconds_since_finished=5)
        self.assertEqual(p.display, VISIBLE_RECENT)
        self.assertEqual(p.lifecycle, RECENT)
        self.assertTrue(p.is_visible)
        self.assertFalse(p.is_open)

    def test_it_retires_once_the_retention_passes(self) -> None:
        p = classify("completed",
                     seconds_since_finished=RECENT_RETENTION_S + 1)
        self.assertEqual(p.lifecycle, RETIRED)
        self.assertFalse(p.is_visible)


class HiddenTest(unittest.TestCase):
    def test_hiding_a_card_closes_it_without_killing_the_worker(self) -> None:
        p = classify("running", hidden_by_user=True)
        self.assertFalse(p.is_open)
        self.assertEqual(p.lifecycle, CURRENT)     # the work continues
        self.assertFalse(is_terminal(p.execution))

    def test_hidden_running_work_is_counted_separately(self) -> None:
        counts = roster([classify("running"),
                         classify("waiting_for_approval"),
                         classify("running", hidden_by_user=True)])
        self.assertEqual(counts["open"], 2)
        self.assertEqual(counts["hidden_running"], 1)


class RosterTest(unittest.TestCase):
    def test_the_spec_s_worked_example(self) -> None:
        """Section 4: three cards, two of them open."""
        counts = roster([classify("running"),
                         classify("running"),
                         classify("completed", seconds_since_finished=10)])
        self.assertEqual(counts["open"], 2)
        self.assertEqual(counts["visible_cards"], 3)

    def test_twenty_sessions_seventeen_finished(self) -> None:
        """Section 22: history must not inflate the open count."""
        live = [classify("running"), classify("waiting_for_approval"),
                classify("running")]
        recent = [classify("completed", seconds_since_finished=30)]
        history = [classify("completed",
                            seconds_since_finished=RECENT_RETENTION_S + 99,
                            has_provider_session=True) for _ in range(16)]
        counts = roster(live + recent + history)
        self.assertEqual(counts["open"], 3)
        self.assertEqual(counts["visible_cards"], 4)
        self.assertEqual(counts["resumable"], 16)

    def test_the_count_falls_as_work_finishes(self) -> None:
        """Section 15, step by step."""
        states = ["running", "running", "running"]
        self.assertEqual(roster([classify(s) for s in states])["open"], 3)
        states[0] = "completed"
        self.assertEqual(roster([classify(s, seconds_since_finished=1)
                                 for s in states])["open"], 2)
        states[1] = "waiting_for_approval"
        self.assertEqual(roster([classify(s, seconds_since_finished=1)
                                 for s in states])["open"], 2)


class ConductorRosterTest(unittest.TestCase):
    """Section 22, against the real conductor rather than the model alone."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        from conductor.global_conductor import GlobalConductor
        from conductor.testing import (FakeCodingAgentRuntime,
                                       FakeWorkspaceManager)
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.roots = Path(self.tmp.name) / "code"
        (self.roots / "proj" / ".git").mkdir(parents=True)
        self.runtime = FakeCodingAgentRuntime()
        self.conductor = GlobalConductor(
            home=self.home, runtime=self.runtime, search_roots=[self.roots],
            workspace_factory=lambda project: FakeWorkspaceManager())
        self.project = self.conductor.locator.register(
            str(self.roots / "proj")).id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make(self, title, status):
        import asyncio
        task = asyncio.run(self.conductor.create_task(
            title, "goal", project_id=self.project))
        self.conductor._conductor(self.project).store.update(
            task.id, status=status)
        return task

    def test_history_does_not_inflate_the_open_count(self) -> None:
        import asyncio
        for i in range(16):
            self.make(f"old {i}", "completed")
        self.make("A", "running")
        self.make("B", "waiting_for_user")
        self.make("C", "running")

        counts = self.conductor.session_counts()
        self.assertEqual(counts["open"], 3,
                         "finished sessions leaked into the open roster")
        self.assertEqual(len(asyncio.run(
            self.conductor.list_open_sessions())), 3)
        self.assertGreaterEqual(counts["resumable"], 16)

    def test_old_work_stays_findable(self) -> None:
        self.make("Login fix", "completed")
        self.make("A", "running")
        found = self.conductor.search_sessions("login")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["title"], "Login fix")

    def test_search_never_returns_open_sessions(self) -> None:
        self.make("Live one", "running")
        self.assertEqual(self.conductor.search_sessions("live"), [])

    def test_a_restart_does_not_reopen_history(self) -> None:
        """The failure this whole change is about: everything resumable
        coming back as open."""
        from pathlib import Path
        from conductor.global_conductor import GlobalConductor
        from conductor.testing import (FakeCodingAgentRuntime,
                                       FakeWorkspaceManager)
        for i in range(12):
            self.make(f"old {i}", "completed")
        self.make("still going", "running")

        revived = GlobalConductor(
            home=self.home, runtime=FakeCodingAgentRuntime(),
            search_roots=[self.roots],
            workspace_factory=lambda project: FakeWorkspaceManager())
        self.assertEqual(revived.session_counts()["open"], 1,
                         "a restart reopened finished sessions")
