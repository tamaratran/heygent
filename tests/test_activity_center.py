"""Notification model inversion tests (spec section 34): the Activity Center
is the persistent supervisor view, OS popups are selective interruptions,
and agent telemetry stays telemetry.

Run with:  python3 -m unittest tests.test_activity_center -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.agent_events import AgentEvent
from conductor.global_conductor import GlobalConductor
from conductor.notifications import (NotificationPolicy, NotificationService,
                                     TaskNotification, concise)
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


class ActivityWorld(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = base / "code"
        (self.roots / "posely" / ".git").mkdir(parents=True)
        self.runtime = FakeCodingAgentRuntime()
        self.conductor = GlobalConductor(
            home=base / "home", runtime=self.runtime,
            search_roots=[self.roots],
            workspace_factory=lambda project: FakeWorkspaceManager())
        self.popups: list[TaskNotification] = []
        self.resolved: list[str] = []
        self.service = NotificationService(
            self.conductor, on_notify=self.popups.append,
            on_resolve=self.resolved.append)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def make_task(self, title: str = "Fix login redirect"):
        registered = self.conductor.locator.register(self.roots / "posely")
        return self.run_async(self.conductor.create_task(
            title, "goal", project_id=registered.id))

    def rows(self, section: str) -> list[dict]:
        snapshot = self.service.snapshot()
        return next(s["groups"] for s in snapshot["sections"]
                    if s["name"] == section)


class ProgressSpamTest(ActivityWorld):
    def test_100_progress_events_one_row_zero_popups(self) -> None:
        task = self.make_task()
        for i in range(100):
            self.runtime.emit(task.provider_session_id, AgentEvent(
                type="checkpoint", summary=f"Running tests (pass {i})"))
        self.assertEqual(self.popups, [])          # zero OS interruptions
        active = self.rows("ACTIVE")
        self.assertEqual(len(active), 1)           # one worker row
        self.assertIn("pass 99", active[0]["activity"])   # mutated in place
        self.assertIn("Fix login redirect", active[0]["task_title"])

    def test_turn_completions_do_not_masquerade_as_task_completion(self):
        task = self.make_task()
        sid = task.provider_session_id
        for i in range(20):
            self.runtime.emit(sid, AgentEvent(type="completed",
                                              summary=f"turn {i} done"))
        completions = [p for p in self.popups if p.type == "completed"]
        self.assertEqual(len(completions), 1)      # first finish notifies...
        stored = [n for n in self.service.store.list(task_id=task.id)
                  if n.type == "completed"]
        self.assertEqual(len(stored), 1)           # ...and only once, ever


class ApprovalAttentionTest(ActivityWorld):
    def test_one_approval_one_item_one_popup_despite_replays(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        for _ in range(10):     # detection + reconciliation re-observations
            self.runtime.pending.setdefault(sid, {})["appr_1"] = {
                "approval_id": "appr_1"}
            self.runtime.emit(sid, AgentEvent(
                type="approval_required",
                question="Install/update the Stripe dependency",
                detail={"approval": {"approval_id": "appr_1"}}))
        popups = [p for p in self.popups if p.type == "needs_input"]
        self.assertEqual(len(popups), 1)
        needs = self.rows("NEEDS ATTENTION")
        self.assertEqual(len(needs), 1)
        self.assertEqual(needs[0]["status"], "waiting_for_approval")
        self.assertIn("Stripe", needs[0]["activity"])

    def test_direct_resolution_clears_attention(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit_approval(sid, "appr_2", "risky thing")
        self.assertEqual(len(self.rows("NEEDS ATTENTION")), 1)
        # The user answers inside the session; the runtime acknowledges.
        self.runtime.emit(sid, AgentEvent(
            type="approval_resolved",
            detail={"approval_id": "appr_2", "decision": "approved",
                    "resolved_by": "user_direct"}))
        del self.runtime.pending[sid]["appr_2"]
        self.assertEqual(self.rows("NEEDS ATTENTION"), [])
        self.assertEqual(len(self.resolved), 1)    # on-screen row retired
        # No stale unread approval remains.
        stale = [n for n in self.service.store.list(task_id=task.id,
                                                    unread_only=True)
                 if n.type == "needs_input"]
        self.assertEqual(stale, [])


class CompletionTest(ActivityWorld):
    def test_completion_once_and_restart_never_renotifies(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit(sid, AgentEvent(type="completed",
                                          summary="Fixed the race."))
        self.assertEqual(
            len([p for p in self.popups if p.type == "completed"]), 1)
        # Restart: a fresh service over the same durable store replays the
        # completion event during reconciliation.
        popups2: list[TaskNotification] = []
        service2 = NotificationService(self.conductor,
                                       on_notify=popups2.append)
        self.runtime.emit(sid, AgentEvent(type="completed",
                                          summary="Fixed the race."))
        self.assertEqual([p for p in popups2 if p.type == "completed"], [])
        service2.close()
        # The worker moved to RECENT, not stuck in ACTIVE forever.
        self.assertIn(task.id, [r["task_id"] for r in self.rows("RECENT")])


class SectionsTest(ActivityWorld):
    def test_five_workers_five_rows_correct_sections(self) -> None:
        working = self.make_task("A working")
        blocked = self.make_task("B approval")
        self.runtime.emit_approval(blocked.provider_session_id, "appr_b",
                                   "Install Stripe")
        paused = self.make_task("C paused")
        self.run_async(self.conductor.pause_task(paused.id))
        done = self.make_task("D done")
        self.runtime.emit(done.provider_session_id, AgentEvent(
            type="completed", summary="done"))
        failed = self.make_task("E failed")
        self.runtime.emit(failed.provider_session_id, AgentEvent(
            type="failed", error="boom"))

        snapshot = self.service.snapshot()
        by_section = {s["name"]: [r["task_title"] for r in s["groups"]]
                      for s in snapshot["sections"]}
        self.assertIn("B approval", by_section["NEEDS ATTENTION"])
        self.assertIn("A working", by_section["ACTIVE"])
        self.assertIn("C paused", by_section["PAUSED"])
        self.assertIn("D done", by_section["RECENT"])
        self.assertIn("E failed", by_section["RECENT"])
        total_rows = sum(len(rows) for rows in by_section.values())
        self.assertEqual(total_rows, 5)            # rows, not event cards

    def test_policy_is_the_single_gate(self) -> None:
        policy = NotificationPolicy()
        self.assertFalse(policy.presents("progress"))
        self.assertFalse(policy.presents("milestone"))
        self.assertFalse(policy.presents("info"))
        self.assertTrue(policy.presents("needs_input"))
        self.assertTrue(policy.presents("completed"))
        self.assertTrue(policy.presents("failed"))

    def test_concise_bodies(self) -> None:
        long = "Search real repo for last-message timestamp fields | " * 8
        short = concise(long)
        self.assertLessEqual(len(short), 140)
        self.assertTrue(short.endswith("…"))


if __name__ == "__main__":
    unittest.main()


class SessionRosterTest(ActivityWorld):
    """The panel is the sessions you have open, not a list of finishes.

    Cards were keyed on the notification, so every event stacked a new one
    and a worker only appeared once it had already completed something. A
    card is a session now: it appears when the worker starts, follows what
    it is doing, and turns into a tick when it finishes.
    """

    def cards(self, notifications):
        """What the panel would hold, keyed the way the UI keys it."""
        panel = {}
        for n in notifications:
            panel[n.task_id or n.id] = n
        return panel

    def test_a_worker_gets_one_row_for_its_whole_life(self) -> None:
        seen = []
        self.service.on_activity = seen.append
        task = self.make_task()
        sid = task.provider_session_id
        for summary in ("Reading the file", "Editing it", "Running the tests"):
            self.runtime.emit(sid, AgentEvent(type="progress",
                                              summary=summary))
        self.runtime.emit(sid, AgentEvent(type="completed",
                                          summary="All done."))
        self.assertGreater(len(seen), 1, "no live updates reached the row")
        self.assertEqual(len(self.cards(seen)), 1,
                         "the session was spread across several cards")

    def test_updates_do_not_interrupt(self) -> None:
        """Only the moments worth breaking into become popups."""
        before = len(self.popups)
        task = self.make_task()
        sid = task.provider_session_id
        for i in range(8):
            self.runtime.emit(sid, AgentEvent(type="progress",
                                              summary=f"step {i}"))
        self.assertEqual(len(self.popups), before,
                         "routine progress interrupted the user")


class RecentIsThisRun(ActivityWorld):
    def test_what_finished_before_this_run_is_not_recent(self) -> None:
        """Measured: RECENT was the last eight finishes of all time - five
        days of sessions the user had moved on from, on every launch."""
        task = self.make_task()
        self.runtime.emit(task.provider_session_id,
                          AgentEvent(type="completed", summary="Fixed."))
        self.assertIn(task.id, [r["task_id"] for r in self.rows("RECENT")])
        # A later launch over the same store.
        self.conductor.started_at = "2999-01-01T00:00:00Z"
        self.assertNotIn(task.id, [r["task_id"] for r in self.rows("RECENT")])

    def test_a_blocked_worker_shows_whatever_its_age(self) -> None:
        task = self.make_task()
        self.runtime.emit(task.provider_session_id,
                          AgentEvent(type="needs_input", summary="which branch?"))
        self.conductor.started_at = "2999-01-01T00:00:00Z"
        self.assertIn(task.id,
                      [r["task_id"] for r in self.rows("NEEDS ATTENTION")])
