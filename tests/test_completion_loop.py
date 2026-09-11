"""The completion loop hard invariant: a user-facing Managed Subagent that
finishes produces exactly one durable user-visible completion notification -
tied to that exact task/subagent/session, from the runtime lifecycle event -
and one structured result the Manager already has.

Run with:  python3 -m unittest tests.test_completion_loop -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.agent_events import AgentEvent
from conductor.global_conductor import GlobalConductor
from conductor.notifications import (NotificationService, NotificationStore,
                                     TaskNotification)
from conductor.notification_panel import (NotificationPanel,
                                          notice_payload)
from conductor.surfaces import FakeSurface
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


class CompletionWorld(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = base / "code"
        (self.roots / "posely" / ".git").mkdir(parents=True)
        self.runtime = FakeCodingAgentRuntime()
        self.surface = FakeSurface()
        self.conductor = GlobalConductor(
            home=base / "home", runtime=self.runtime,
            search_roots=[self.roots], surface=self.surface,
            workspace_factory=lambda project: FakeWorkspaceManager())
        self.popups: list[TaskNotification] = []
        self.service = NotificationService(self.conductor,
                                           on_notify=self.popups.append)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def make_task(self, title: str = "Fix login redirect"):
        registered = self.conductor.locator.register(self.roots / "posely")
        return self.run_async(self.conductor.create_task(
            title, "goal", project_id=registered.id))

    def completions(self, task_id: str) -> list[TaskNotification]:
        return [n for n in self.service.store.list(task_id=task_id)
                if n.type == "completed"]


class HardInvariantTest(CompletionWorld):
    def test_completion_notifies_with_exact_identity(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        # The worker checkpoints a warning, then finishes.
        self.runtime.emit(sid, AgentEvent(
            type="checkpoint", summary="Retry logic fixed",
            detail={"warnings": ["One legacy integration still needs "
                                 "manual verification"]}))
        self.runtime.emit(sid, AgentEvent(
            type="completed",
            summary="Webhook retry logic fixed. Tests passed."))

        completions = self.completions(task.id)
        self.assertEqual(len(completions), 1)
        note = completions[0]
        # Stable identity, tied to the exact worker that produced it.
        self.assertEqual(note.task_id, task.id)
        self.assertEqual(note.subagent_id, f"sub_{task.id}")
        self.assertEqual(note.provider_session_id, sid)
        self.assertEqual(note.project_id, task.project_id)
        # Content: result summary plus warnings, per the spec's example.
        self.assertIn("✓", note.title)
        self.assertIn("Webhook retry logic fixed", note.body)
        self.assertIn("Warning:", note.body)
        self.assertIn("manual verification", note.body)
        # It popped up (completion is always popup-worthy).
        self.assertIn("completed", [p.type for p in self.popups])

    def test_manager_already_knows_without_asking(self) -> None:
        task = self.make_task()
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="completed",
            summary="Fixed the redirect race. 12 auth tests passed."))
        # "Did the login one finish?" is answerable from state alone.
        listing = self.run_async(self.conductor.list_subagents())
        entry = next(e for e in listing if e["task_id"] == task.id)
        self.assertIn("Fixed the redirect race", entry["result_summary"])
        subagent = self.run_async(self.conductor.subagent_for(task.id))
        self.assertTrue(subagent.result.success)

    def test_replayed_completion_notifies_exactly_once(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        for _ in range(3):     # completed / completed / reconcile->completed
            self.runtime.emit(sid, AgentEvent(
                type="completed", summary="Fixed the redirect race."))
        self.assertEqual(len(self.completions(task.id)), 1)
        self.assertEqual(
            len([p for p in self.popups if p.type == "completed"]), 1)

    def test_each_thing_the_user_asks_for_is_reported(self) -> None:
        """One notification per instruction answered.

        This asserted the opposite - one completion per task, ever - which
        in a live session meant the worker reported that it was oriented and
        then finished the actual work in silence. A PTY worker stays alive
        and ends a turn for every instruction, so a completion that answers
        something the user sent is news; a turn end with no instruction
        behind it is the worker talking to itself.
        """
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit(sid, AgentEvent(type="completed",
                                          summary="First unit done."))
        self.assertEqual(len(self.completions(task.id)), 1)
        self.run_async(self.conductor.send_to_task(task.id, "now part two"))
        self.runtime.emit(sid, AgentEvent(type="completed",
                                          summary="Second unit done."))
        self.assertEqual(len(self.completions(task.id)), 2)
        subagent = self.run_async(self.conductor.subagent_for(task.id))
        self.assertIn("Second unit done", subagent.result.summary)

    def test_notification_survives_restart_durably(self) -> None:
        task = self.make_task()
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="completed", summary="Done."))
        reloaded = NotificationStore(self.conductor.projects.home)
        kept = [n for n in reloaded.list(task_id=task.id)
                if n.type == "completed"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].provider_session_id,
                         task.provider_session_id)

    def test_click_path_targets_the_same_worker(self) -> None:
        """The notification's identity feeds focus_task: same task, same
        session, no replacement worker."""
        task = self.make_task()
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="completed", summary="Done."))
        note = self.completions(task.id)[0]
        before = self.runtime._counter
        self.run_async(self.conductor.focus_task(note.task_id))
        self.assertEqual(self.runtime._counter, before)   # no new worker
        executions = self.run_async(self.conductor.executions())
        self.assertEqual(executions[0].provider_session_id,
                         note.provider_session_id)

    def test_failure_also_notifies_with_identity(self) -> None:
        task = self.make_task()
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="failed", error="tests would not pass"))
        failures = [n for n in self.service.store.list(task_id=task.id)
                    if n.type == "failed"]
        self.assertEqual(len(failures), 1)
        subagent = self.run_async(self.conductor.subagent_for(task.id))
        self.assertFalse(subagent.result.success)


if __name__ == "__main__":
    unittest.main()


class NotifyPerInstructionTest(HardInvariantTest):
    """The reported failure: a worker reports that it is oriented, then
    finishes the real work in silence."""

    def test_chatter_without_an_instruction_stays_quiet(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        for i in range(20):
            self.runtime.emit(sid, AgentEvent(type="completed",
                                              summary=f"turn {i} done"))
        self.assertEqual(len(self.completions(task.id)), 1,
                         "turn ends with nothing behind them spammed the user")

    def test_three_instructions_three_reports(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit(sid, AgentEvent(type="completed",
                                          summary="Oriented and standing by."))
        for i, msg in enumerate(("check the tests", "commit it", "push it"), 2):
            self.run_async(self.conductor.send_to_task(task.id, msg))
            self.runtime.emit(sid, AgentEvent(type="completed",
                                              summary=f"Done: {msg}"))
            self.assertEqual(len(self.completions(task.id)), i,
                             f"no notification after {msg!r}")

    def test_a_replayed_completion_does_not_re_notify(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        self.run_async(self.conductor.send_to_task(task.id, "do the thing"))
        for _ in range(5):        # reconciliation re-observing the same end
            self.runtime.emit(sid, AgentEvent(type="completed",
                                              summary="Done the thing."))
        self.assertEqual(len(self.completions(task.id)), 1)


class CardTextFollowsTheWork(CompletionWorld):
    """The card body must change when a worker answers a follow-up.

    A PTY worker stays alive across turns, and the card is keyed on the
    task, so turn two has to rewrite the row rather than leave turn one's
    text sitting there. Two separate things had to hold for that: the
    service must emit a notification for the later turn at all (it used to
    dedupe on the task and swallow it), and the payload must land on the
    same card id so the panel updates in place instead of stacking.
    """

    def setUp(self) -> None:
        super().setUp()
        self.panel = NotificationPanel()
        self.service.on_notify = self.record

    def record(self, notification) -> None:
        self.popups.append(notification)
        self.panel.upsert(notice_payload(notification, force=True))

    def bodies(self) -> list[str]:
        return [item["body"] for item in self.panel.items]

    def turn(self, task, instruction: str, answer: str) -> None:
        self.run_async(self.conductor.send_to_task(task.id, instruction))
        self.runtime.emit(task.provider_session_id,
                          AgentEvent(type="completed", summary=answer))

    def test_a_follow_up_rewrites_the_card_instead_of_stacking(self) -> None:
        task = self.make_task()
        self.turn(task, "orient yourself", "Read the auth module.")
        self.assertEqual(len(self.panel.items), 1)
        self.assertIn("Read the auth module", self.bodies()[0])

        self.turn(task, "now fix the race", "Fixed the race in session.py.")
        self.assertEqual(len(self.panel.items), 1, "the card stacked")
        self.assertIn("Fixed the race", self.bodies()[0],
                      "the card still shows the first turn's text")
        self.assertNotIn("Read the auth module", self.bodies()[0])

    def test_a_third_turn_still_updates(self) -> None:
        task = self.make_task()
        for i, answer in enumerate(["First.", "Second.", "Third."]):
            self.turn(task, f"step {i}", answer)
        self.assertEqual(len(self.panel.items), 1)
        self.assertIn("Third", self.bodies()[0])

    def test_two_workers_keep_their_own_rows(self) -> None:
        a = self.make_task("Auth")
        b = self.make_task("Billing")
        self.turn(a, "go", "Auth done.")
        self.turn(b, "go", "Billing done.")
        self.assertEqual(len(self.panel.items), 2)
        self.assertEqual(sorted(x[:7] for x in self.bodies()),
                         ["Auth do", "Billing"])
