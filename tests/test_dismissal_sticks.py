"""A dismissed card stays dismissed until there is news.

Measured: a card the user waved away came back on every later change to
an idle worker (each re-projection of a finished worker carried force)
and on every restart (the dismissal lived only in the overlay's memory).
Dismissal is now canonical state - "seen up to this revision" - and a
card reopens only for a result or a question that arrived after it.

Run with:  python3 -m unittest tests.test_dismissal_sticks -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conductor.agent_events import AgentEvent
from conductor.notification_panel import NotificationPanel
from conductor.session_card import project_card
from conductor.subagent_state import (SubagentState, SubagentStateStore,
                                      dismiss, reduce)


def fresh() -> SubagentState:
    return SubagentState(id="sub_task_1", task_id="task_1", project_id="p",
                         provider="anthropic", provider_session_id="s",
                         title="Fix login", status="working")


def step(state: SubagentState, event_type: str, seq: int, **kw) -> SubagentState:
    event = AgentEvent(type=event_type, sequence=seq,
                       event_id=f"aev_{event_type}_{seq}", **kw)
    new, transition = reduce(state, event)
    assert transition.outcome == "applied", (event_type, transition)
    return new


class DismissalIsSeenUpToHere(unittest.TestCase):
    def test_a_finish_reopens_a_dismissed_card_once_then_stays_away(self):
        panel = NotificationPanel()
        state = step(fresh(), "completed", 1, summary="tests pass")
        panel.upsert(project_card(state.to_dict()), force=True)
        self.assertEqual([n["id"] for n in panel.items], ["task_1"])
        panel.dismiss("task_1")
        state = dismiss(state)
        # Later ticks of the same, finished worker: a progress line, a
        # re-projection of the same finish. No news.
        for again in (state, step(state, "progress", 2, summary="Reading foo.py")):
            card = project_card(again.to_dict())
            self.assertFalse(card["force"], card)
            self.assertTrue(card["dismissed"])
            panel.upsert(card, force=card["force"])
        self.assertEqual(panel.items, [], "the dismissed card came back")

    def test_a_new_result_after_the_dismissal_is_news(self):
        panel = NotificationPanel()
        state = dismiss(step(fresh(), "completed", 1, summary="first answer"))
        panel.dismiss("task_1")
        state = step(state, "started", 2)
        state = step(state, "completed", 3, summary="second answer")
        card = project_card(state.to_dict())
        self.assertTrue(card["force"])
        self.assertFalse(card["dismissed"])
        panel.upsert(card, force=card["force"])
        self.assertEqual([n["id"] for n in panel.items], ["task_1"])
        self.assertIn("second answer", card["status_text"])

    def test_a_question_after_the_dismissal_is_news(self):
        state = dismiss(step(fresh(), "completed", 1, summary="done"))
        state = step(state, "needs_input", 2, question="Which branch?")
        card = project_card(state.to_dict())
        self.assertTrue(card["force"])
        self.assertTrue(card["requires_attention"])

    def test_a_question_the_user_already_dismissed_stays_dismissed(self):
        state = step(fresh(), "approval_required", 1, question="Run rm?",
                     detail={"approval": {"approval_id": "apr_1"}})
        state = dismiss(state)
        card = project_card(state.to_dict())
        self.assertFalse(card["force"])
        self.assertTrue(card["dismissed"])

    def test_a_failure_after_the_dismissal_is_news(self):
        state = dismiss(step(fresh(), "completed", 1, summary="ok"))
        state = step(state, "started", 2)
        state = step(state, "failed", 3, error="boom")
        self.assertTrue(project_card(state.to_dict())["force"])

    def test_never_dismissed_behaves_as_before(self):
        card = project_card(step(fresh(), "completed", 1, summary="ok").to_dict())
        self.assertTrue(card["force"])
        self.assertFalse(card["dismissed"])
        working = project_card(fresh().to_dict())
        self.assertFalse(working["force"])
        self.assertFalse(working["dismissed"])


class DismissalSurvivesARestart(unittest.TestCase):
    def test_the_sidecar_carries_it_and_a_fresh_panel_honours_it(self):
        tmp = tempfile.TemporaryDirectory()
        store = SubagentStateStore(lambda task_id: Path(tmp.name) / task_id)
        state = dismiss(step(fresh(), "completed", 1, summary="done"))
        store.save(state)
        reloaded = store.get("task_1")
        self.assertEqual(reloaded.dismissed_at_revision, state.revision)
        self.assertEqual(reloaded.result_revision, 1)
        # The overlay restarted: empty memory, the startup re-render
        # projects every visible worker. This one stays away.
        panel = NotificationPanel()
        card = project_card(reloaded.to_dict())
        panel.upsert(card, force=card["force"])
        self.assertEqual(panel.items, [])
        self.assertIn("task_1", panel.dismissed)
        tmp.cleanup()

    def test_a_worker_still_at_revision_zero_can_be_dismissed(self):
        """Measured: the workers that predated the field loaded at
        revision 0 and, idle, stayed there; their dismissal was recorded
        as 0 - "never dismissed" - and the cards came back on every
        restart. A dismissal is a revision of its own."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SubagentStateStore(lambda task_id: Path(tmp.name) / task_id)
        data = fresh().to_dict()
        data["status"] = "idle"
        for key in ("revision", "result_revision", "attention_revision",
                    "dismissed_at_revision"):
            data.pop(key)
        state = SubagentState.from_dict(data)      # an old sidecar, idle
        self.assertEqual(state.revision, 0)
        state = dismiss(state)
        self.assertGreater(state.dismissed_at_revision, 0)
        store.save(state)
        reloaded = store.get("task_1")
        card = project_card(reloaded.to_dict())
        self.assertTrue(card["dismissed"], card)
        self.assertFalse(card["force"], card)
        panel = NotificationPanel()
        panel.upsert(card, force=card["force"])
        self.assertEqual(panel.items, [], "the dismissed card came back")
        # A finish that arrives after it is still news.
        later = step(step(reloaded, "started", 2), "completed", 3,
                     summary="a second run")
        self.assertTrue(project_card(later.to_dict())["force"])

    def test_an_old_sidecar_without_the_fields_still_loads(self):
        data = fresh().to_dict()
        for key in ("revision", "result_revision", "attention_revision",
                    "dismissed_at_revision"):
            data.pop(key)
        state = SubagentState.from_dict(data)
        self.assertEqual(state.dismissed_at_revision, 0)
        self.assertFalse(project_card(state.to_dict())["dismissed"])


class TheConductorRemembersIt(unittest.TestCase):
    """End to end through a real GlobalConductor: the overlay reports a
    dismissal, the conductor records it, and the projected cards say
    so until the worker has news."""

    def test_dismiss_then_ticks_then_a_new_finish(self):
        from tests.test_deterministic_state import World, ev
        world = World("setUp")
        world.setUp()
        try:
            task = world.make_task()
            sid = task.provider_session_id
            world.runtime.emit(sid, ev("completed", summary="first answer"))
            self.assertTrue(world.latest_card(task.id)["force"])
            recorded = world.conductor.dismiss_card(task.id)
            self.assertGreater(recorded["dismissed_at_revision"], 0)
            stored = next(s for s in world.conductor.subagent_states()
                          if s["task_id"] == task.id)
            self.assertEqual(stored["dismissed_at_revision"],
                             recorded["dismissed_at_revision"],
                             "persisted, not just returned")
            world.runtime.emit(sid, ev("progress", summary="Reading foo.py",
                                       detail={"tool": "Read"}))
            card = world.latest_card(task.id)
            self.assertFalse(card["force"])
            self.assertTrue(card["dismissed"])
            world.runtime.emit(sid, ev("completed", summary="second answer"))
            card = world.latest_card(task.id)
            self.assertTrue(card["force"])
            self.assertFalse(card["dismissed"])
            self.assertIsNone(world.conductor.dismiss_card("task_nope"))
        finally:
            world.tearDown()


class TheTrayComesBackInOrder(unittest.TestCase):
    """A restart replays the cards in the order the workers were started,
    whatever order the store happens to hold them in."""

    def test_states_are_listed_by_start_time(self):
        from dataclasses import replace
        from tests.test_deterministic_state import World
        world = World("setUp")
        world.setUp()
        try:
            first = world.make_task("First")
            second = world.make_task("Second")
            project = world.conductor._conductor(first.project_id)
            # The store lists first before second; say first started later.
            def stamp(task, created_at):
                state = project._subagent_state(project.store.get(task.id))
                project.subagents.save(replace(state, created_at=created_at))
            stamp(first, "2030-01-01T00:00:00Z")
            stamp(second, "2020-01-01T00:00:00Z")
            order = [s["task_id"] for s in world.conductor.subagent_states()]
            self.assertEqual(order[:2], [second.id, first.id])
        finally:
            world.tearDown()


if __name__ == "__main__":
    unittest.main()
