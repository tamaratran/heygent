"""Deterministic agent state, cards, and Boss delivery - the acceptance
walks of the spec, as tests.

Two branches leave canonical state and neither depends on the other:

    objective    event -> reduce -> SubagentState -> project_card -> UI
    semantic     meaningful transition -> SupervisorInbox -> Boss -> voice

The invariants asserted here, in the spec's words: canonical state
before presentation; the card is a projection of state and of nothing
else; the Boss is never in the worker->UI path; a NotificationService,
Boss or voice failure cannot make the card wrong; duplicates cannot
produce duplicate effects; stale events cannot rewind terminal state;
speech about several finishes is one sentence.

Run with:  python3 -m unittest tests.test_deterministic_state -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor.agent_events import AgentEvent
from conductor.global_conductor import GlobalConductor
from conductor.notifications import NotificationService
from conductor.session_card import project_card, status_text
from conductor.subagent_state import (SubagentState, apply_lifecycle,
                                      reduce)
from conductor.supervisor_inbox import (SpeechPolicy, SupervisoryEvent,
                                        SupervisorInbox, event_id_for)
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager
from conductor.voice_coordinator import VoiceCoordinator


def fresh(status: str = "starting", **extra) -> SubagentState:
    return SubagentState(id="sub_task_a", task_id="task_a", project_id="p",
                         provider="claude-code", provider_session_id="s1",
                         title="Posely · Fix login", status=status, **extra)


def ev(type_: str, seq: int = 0, **fields) -> AgentEvent:
    return AgentEvent(type=type_, sequence=seq, **fields)


# ----------------------------------------------------------------- reducer
class TheReducerOwnsTheTruth(unittest.TestCase):
    def test_a_worker_walks_starting_working_waiting_working_idle(self):
        s = fresh()
        s, _ = reduce(s, ev("started", 1))
        self.assertEqual(s.status, "working")
        s, t = reduce(s, ev("progress", 2, summary="Bash(pytest -q)",
                            detail={"tool": "Bash"}))
        self.assertEqual((s.status, s.activity), ("working", "running_tests"))
        s, _ = reduce(s, ev("approval_required", 3, question="push changes?",
                            detail={"approval": {"approval_id": "ap1"}}))
        self.assertEqual(s.status, "waiting_for_approval")
        self.assertEqual(s.pending_approval["approval_id"], "ap1")
        s, _ = reduce(s, ev("approval_resolved", 4))
        self.assertEqual((s.status, s.pending_approval), ("working", None))
        s, _ = reduce(s, ev("completed", 5, summary="All auth tests passed."))
        self.assertEqual(s.status, "idle")
        self.assertEqual(s.result["summary"], "All auth tests passed.")

    def test_the_same_event_applied_ten_times_changes_state_once(self):
        s = fresh("working")
        done = ev("completed", 7, summary="Done.")
        outcomes = []
        for _ in range(10):
            s, t = reduce(s, done)
            outcomes.append(t.outcome)
        self.assertEqual(outcomes[0], "applied")
        self.assertEqual(set(outcomes[1:]), {"duplicate"})
        self.assertEqual(s.status, "idle")

    def test_a_stale_event_cannot_rewind_state(self):
        """sequence 20 -> completed, then sequence 19 -> running tests."""
        s = fresh("working")
        s, _ = reduce(s, ev("completed", 20, summary="Done."))
        s, t = reduce(s, ev("progress", 19, summary="Bash(pytest)",
                            detail={"tool": "Bash"}))
        self.assertEqual(t.outcome, "stale")
        self.assertEqual(s.status, "idle")
        self.assertEqual(s.result["summary"], "Done.")

    def test_terminal_state_is_final_for_events(self):
        s = apply_lifecycle(fresh("working"), "completed")
        s, t = reduce(s, ev("progress", 99, summary="late"))
        self.assertEqual(t.outcome, "terminal")
        self.assertEqual(s.status, "completed")
        s, t = reduce(s, ev("completed", 100, summary="straggler"))
        self.assertEqual(t.outcome, "terminal")

    def test_only_a_lifecycle_decision_leaves_a_terminal_state(self):
        s = apply_lifecycle(fresh("working"), "cancelled")
        self.assertEqual(apply_lifecycle(s, "working").status, "working")

    def test_failure_is_a_terminal_status_with_a_result(self):
        s, _ = reduce(fresh("working"), ev("failed", 3, error="migration tests failed"))
        self.assertEqual(s.status, "failed")
        self.assertFalse(s.result["success"])
        self.assertIsNotNone(s.completed_at)

    def test_reduce_is_pure(self):
        before = fresh("working")
        reduce(before, ev("completed", 1, summary="x"))
        self.assertEqual(before.status, "working")
        self.assertIsNone(before.result)

    def test_state_round_trips_through_disk_shape(self):
        s, _ = reduce(fresh(), ev("approval_required", 1, question="q",
                                  detail={"approval": {"approval_id": "a"}}))
        again = SubagentState.from_dict(s.to_dict())
        self.assertEqual(again, s)


# ---------------------------------------------------------------- projector
class TheCardIsAProjection(unittest.TestCase):
    def card(self, status, **extra):
        return project_card(fresh(status, **extra).to_dict())

    def test_every_status_has_a_card(self):
        from conductor.subagents import SUBAGENT_STATUSES
        for status in SUBAGENT_STATUSES:
            card = self.card(status)
            self.assertTrue(card["body"], status)
            self.assertIn(card["glyph"], ("working", "done", "attention", "failed"))

    def test_one_card_per_worker_keyed_on_the_task(self):
        self.assertEqual(self.card("working")["id"], "task_a")
        self.assertEqual(self.card("idle", result={"summary": "ok"})["id"], "task_a")

    def test_the_lifecycle_reads_as_the_spec_shows_it(self):
        self.assertEqual(status_text(fresh().to_dict()), "Starting…")
        working = fresh("working", activity="running_tests",
                        activity_text="pytest -q")
        self.assertEqual(status_text(working.to_dict()), "Running tests — pytest -q")
        waiting = fresh("waiting_for_approval",
                        pending_approval={"question": "push changes?"})
        self.assertEqual(status_text(waiting.to_dict()),
                         "Needs your approval — push changes?")
        done = fresh("idle", result={"summary": "All auth tests passed."})
        self.assertEqual(status_text(done.to_dict()),
                         "Answered — All auth tests passed")
        failed = fresh("failed", result={"summary": "migration tests failed"})
        self.assertEqual(status_text(failed.to_dict()),
                         "Failed — migration tests failed")

    def test_attention_styling_is_deterministic(self):
        self.assertEqual(self.card("waiting_for_approval")["glyph"], "attention")
        self.assertEqual(self.card("waiting_for_input")["glyph"], "attention")
        self.assertEqual(self.card("failed")["glyph"], "failed")
        self.assertEqual(self.card("idle", result={"summary": "x"})["glyph"], "done")
        self.assertEqual(self.card("working")["glyph"], "working")
        self.assertTrue(self.card("waiting_for_input")["requires_attention"])
        self.assertFalse(self.card("working")["requires_attention"])

    def test_prose_does_not_reach_the_card(self):
        s, _ = reduce(fresh("working"), ev(
            "progress", 1,
            summary="I'm going to look through the implementation and then I'll probably check the existing tests and see what else"))
        text = status_text(s.to_dict())
        self.assertLessEqual(len(text), 75)
        self.assertTrue(text.startswith("Working"))


# ---------------------------------------------------- the world, end to end
class World(unittest.TestCase):
    """A real GlobalConductor with a fake runtime: events flow through the
    reducer, the bus and the notification service exactly as in the
    product, minus the overlay."""

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
        self.cards: list[dict] = []
        self.state_events = []
        self.conductor.bus.subscribe(self._project)
        self.inbox = SupervisorInbox(base / "home" / "inbox.jsonl")
        self.conductor.inbox = self.inbox

    def _project(self, event) -> None:
        if event.type == "subagent.state_changed":
            self.state_events.append(event)
            self.cards.append(project_card(event.data["state"]))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_task(self, title="Fix login redirect"):
        registered = self.conductor.locator.register(self.roots / "posely")
        return asyncio.run(self.conductor.create_task(title, "goal",
                                                      project_id=registered.id))

    def latest_card(self, task_id: str) -> dict:
        return next(c for c in reversed(self.cards) if c["task_id"] == task_id)


class CompletionAcceptance(World):
    def test_the_card_follows_the_worker_without_the_boss(self):
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit(sid, ev("progress", summary="Bash(pytest -q tests/)",
                                  detail={"tool": "Bash"}))
        self.assertEqual(self.latest_card(task.id)["body"],
                         "Running tests — pytest -q tests/")
        self.runtime.emit(sid, ev("completed", summary="All auth tests passed."))
        card = self.latest_card(task.id)
        self.assertEqual(card["glyph"], "done")
        self.assertIn("Answered", card["body"])
        # No Manager was ever consulted: the conductor has none.
        self.assertIsNone(self.conductor.manager)

    def test_canonical_state_is_persisted_and_restored(self):
        task = self.make_task()
        self.runtime.emit(task.provider_session_id,
                          ev("completed", summary="Done the thing."))
        inner = self.conductor._conductor(task.project_id)
        inner.subagents.forget(task.id)          # drop the cache: read disk
        state = inner.subagent_state_dict(task.id)
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state["result"]["summary"], "Done the thing.")
        restored = self.conductor.subagent_states()
        self.assertEqual([s["task_id"] for s in restored], [task.id])

    def test_a_notification_service_failure_cannot_stale_the_card(self):
        task = self.make_task()
        broken = NotificationService(self.conductor, on_notify=lambda n: 1 / 0,
                                     on_activity=lambda n: 1 / 0)
        self.addCleanup(broken.close)
        self.runtime.emit(task.provider_session_id,
                          ev("completed", summary="Finished anyway."))
        self.assertEqual(self.latest_card(task.id)["glyph"], "done")

    def test_ten_replays_make_one_transition_and_one_card_change(self):
        task = self.make_task()
        done = ev("completed", summary="Done.")
        before = len(self.cards)
        for _ in range(10):
            self.runtime.emit(task.provider_session_id, done)
        self.assertEqual(len(self.cards) - before, 1)
        dropped = [e for e in self.state_events]      # only applied ones
        self.assertEqual(len([e for e in dropped if e.task_id == task.id
                              and e.data["transition"]["kind"] == "completed"]), 1)

    def test_a_late_progress_event_does_not_rewind_a_finish(self):
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit(sid, ev("completed", seq=20, summary="Done."))
        self.runtime.emit(sid, ev("progress", seq=19, summary="Bash(pytest)",
                                  detail={"tool": "Bash"}))
        self.assertEqual(self.latest_card(task.id)["glyph"], "done")

    def test_closing_the_task_moves_canonical_state_through_the_store(self):
        """Lifecycle decisions reach the reducer through the store's one
        status seam, whichever of the nineteen callers made them."""
        task = self.make_task()
        asyncio.run(self.conductor.complete_task(task.id))
        self.assertEqual(self.latest_card(task.id)["state"], "completed")
        self.runtime.emit(task.provider_session_id,
                          ev("completed", summary="late straggler"))
        self.assertEqual(self.latest_card(task.id)["state"], "completed")


class ApprovalAcceptance(World):
    def test_a_sensitive_approval_is_attention_on_the_card(self):
        task = self.make_task()
        self.runtime.emit(task.provider_session_id, ev(
            "approval_required", question="push changes to main?",
            detail={"approval": {"approval_id": "ap1"}}))
        card = self.latest_card(task.id)
        self.assertEqual(card["glyph"], "attention")
        self.assertIn("push changes to main?", card["body"])
        self.runtime.emit(task.provider_session_id, ev(
            "approval_resolved", detail={"approval_id": "ap1",
                                         "decision": "approved"}))
        self.assertEqual(self.latest_card(task.id)["glyph"], "working")


class MultiAgentAcceptance(World):
    def test_three_workers_three_independent_cards(self):
        a, b, c = (self.make_task(t) for t in ("Posely", "Billing", "Test app"))
        self.runtime.emit(a.provider_session_id, ev(
            "progress", summary="Bash(pytest)", detail={"tool": "Bash"}))
        self.runtime.emit(b.provider_session_id, ev(
            "approval_required", question="push?",
            detail={"approval": {"approval_id": "x"}}))
        self.runtime.emit(c.provider_session_id, ev("completed", summary="Shipped."))
        self.assertEqual(self.latest_card(a.id)["glyph"], "working")
        self.assertEqual(self.latest_card(b.id)["glyph"], "attention")
        self.assertEqual(self.latest_card(c.id)["glyph"], "done")


# ------------------------------------------------------------------ inbox
def sup(type_, task_id="task_a", summary="Done.", title="Posely", **extra):
    return SupervisoryEvent(event_id=event_id_for(extra.pop("src", ""), type_)
                            if "src" in extra else f"sup_{type_}_{task_id}_{abs(hash(summary)) % 10**6}",
                            task_id=task_id, subagent_id=f"sub_{task_id}",
                            type=type_, summary=summary,
                            requires_action=type_ in ("approval_required", "input_required"),
                            task_title=title, **extra)


class OneTaskOneFinish(unittest.TestCase):
    """Heard live: "Two tasks just finished: Was something with FFX just
    put in? and Was something with FFX just put in?" One worker, one
    turn, two text blocks - two completed events twenty milliseconds
    apart, and the voice counted them as two tasks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = SupervisorInbox(Path(self.tmp.name) / "inbox.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_finishes_of_one_task_are_one_piece_of_news(self):
        from conductor.supervisor_inbox import SpeechPolicy
        self.inbox.offer(sup("completed", src="evt_1",
                             summary="I'll fetch origin, then scan."))
        self.inbox.offer(sup("completed", src="evt_2",
                             summary="No - nothing mentioning FFX has landed."))
        said = SpeechPolicy().decide(self.inbox)
        self.assertEqual(len(said), 1)
        self.assertNotIn("tasks just finished", said[0].text)
        # The later finish is the one with the answer in it.
        self.assertIn("FFX", said[0].text)
        first = self.inbox.record(event_id_for("evt_1", "completed"))
        self.assertEqual(first.voice_decision, "superseded")

    def test_two_tasks_finishing_together_are_still_one_sentence(self):
        from conductor.supervisor_inbox import SpeechPolicy
        self.inbox.offer(sup("completed", src="evt_1", task_id="task_a",
                             title="Login", summary="Done."))
        self.inbox.offer(sup("completed", src="evt_2", task_id="task_b",
                             title="Billing", summary="Shipped."))
        said = SpeechPolicy().decide(self.inbox)
        self.assertEqual(len(said), 1)
        self.assertIn("Two tasks just finished", said[0].text)


class TheInboxDeliversOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "inbox.jsonl"
        self.inbox = SupervisorInbox(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_same_source_event_derives_the_same_id(self):
        self.assertEqual(event_id_for("evt_1", "completed"),
                         event_id_for("evt_1", "completed"))
        self.assertNotEqual(event_id_for("evt_1", "completed"),
                            event_id_for("evt_2", "completed"))

    def test_a_replayed_event_is_offered_once(self):
        event = sup("completed", src="evt_1")
        self.assertTrue(self.inbox.offer(event))
        for _ in range(9):
            self.assertFalse(self.inbox.offer(event))
        self.assertEqual(len(self.inbox.events()), 1)

    def test_the_same_fact_under_a_new_id_is_one_fact(self):
        self.assertTrue(self.inbox.offer(sup("completed", summary="Tests passed.")))
        self.assertFalse(self.inbox.offer(sup("completed", summary="tests  passed.")))

    def test_pending_survives_a_restart_and_acks_do_too(self):
        self.inbox.offer(sup("approval_required", summary="push?"))
        self.inbox.offer(sup("completed", summary="Done."))
        self.inbox.ack_manager([self.inbox.events()[1].event_id])
        again = SupervisorInbox(self.path)
        pending = again.pending_for_manager()
        self.assertEqual([e.type for e in pending], ["approval_required"])

    def test_the_manager_digest_leads_with_decisions(self):
        self.inbox.offer(sup("completed", summary="Done."))
        self.inbox.offer(sup("approval_required", task_id="task_b",
                             summary="push?", title="Billing"))
        digest = self.inbox.digest_for_manager()
        self.assertTrue(digest.startswith("SINCE YOUR LAST TURN:"))
        lines = digest.splitlines()
        self.assertIn("approval_required", lines[1])
        self.assertIn("completed", lines[2])

    def test_delivery_records_answer_what_happened(self):
        event = sup("completed")
        self.inbox.offer(event)
        self.inbox.record_voice(event.event_id, "coalesced", "with 2 others")
        self.inbox.record_notification_for("task_a")
        record = SupervisorInbox(self.path).record(event.event_id)
        self.assertEqual(record.voice_decision, "coalesced")
        self.assertTrue(record.notification_recorded)
        self.assertFalse(record.manager_delivered)

    def test_a_turn_acks_only_what_the_manager_actually_read(self):
        """The digest rides in global_context, which only a headless
        manager reads. A manager that is typed its updates acks each on
        the push that lands; a turn must not mark delivered what was
        never typed into its window."""
        from conductor.manager import ManagerTurn

        def conductor_with(manager):
            g = GlobalConductor.__new__(GlobalConductor)
            g._open_turns, g._utterances = 0, []
            g.inbox = self.inbox
            g._emit = lambda *a, **k: None
            g.list_tasks = lambda: []
            g.list_projects = lambda: []
            g.manager = manager
            return g

        class Pushed:
            async def handle(self, text, conductor):
                return ManagerTurn(reply="ok")

            def deliver_supervisory(self, event):
                return True

        class Headless:
            async def handle(self, text, conductor):
                return ManagerTurn(reply="ok")

        self.inbox.offer(sup("completed", summary="Done."))
        asyncio.run(conductor_with(Pushed())._turn("hi", "text", "", ""))
        self.assertEqual(len(self.inbox.pending_for_manager()), 1,
                         "acked what was never typed into the window")
        asyncio.run(conductor_with(Headless())._turn("hi", "text", "", ""))
        self.assertEqual(self.inbox.pending_for_manager(), [])

    def test_an_answered_approval_is_withdrawn(self):
        event = sup("approval_required", summary="push?")
        self.inbox.offer(event)
        self.assertEqual(self.inbox.withdraw("task_a", ("approval_required",),
                                             "resolved by policy"), 1)
        self.assertEqual(self.inbox.pending_for_voice(), [])
        self.assertEqual(self.inbox.pending_for_manager(), [])
        self.assertEqual(self.inbox.record(event.event_id).voice_decision,
                         "suppressed_stale")


class TheBossDecidesWhatToSay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = SupervisorInbox(Path(self.tmp.name) / "inbox.jsonl")
        self.policy = SpeechPolicy(mode="important")

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_finishes_are_one_sentence(self):
        for name in ("Posely login", "billing", "the test app"):
            self.inbox.offer(sup("completed", task_id=name.replace(" ", "_"),
                                 title=name, summary=f"{name} done"))
        utterances = self.policy.decide(self.inbox)
        self.assertEqual(len(utterances), 1)
        self.assertEqual(utterances[0].text,
                         "Three tasks just finished: Posely login, billing, and the test app.")
        for event in self.inbox.events():
            self.assertEqual(self.inbox.record(event.event_id).voice_decision,
                             "coalesced")

    def test_one_finish_is_said_plainly(self):
        self.inbox.offer(sup("completed", summary="All tests passed."))
        [u] = self.policy.decide(self.inbox)
        self.assertEqual(u.text, "The Posely agent finished. All tests passed.")
        self.assertEqual(u.kind, "completed")

    def test_what_the_conversation_already_covered_is_not_repeated(self):
        """User: "Did Posely finish?" Boss: "Yes, it just did." - then the
        proactive announcement is suppressed, and says why."""
        event = sup("completed")
        self.inbox.offer(event)
        self.assertEqual(self.policy.decide(self.inbox, covered_subjects={"task_a"}), [])
        self.assertEqual(self.inbox.record(event.event_id).voice_decision,
                         "suppressed_context")

    def test_silent_mode_records_the_policy_not_silence(self):
        event = sup("failed", summary="boom")
        self.inbox.offer(event)
        self.assertEqual(SpeechPolicy(mode="silent").decide(self.inbox), [])
        self.assertEqual(self.inbox.record(event.event_id).voice_decision,
                         "suppressed_policy")

    def test_an_approval_outranks_a_finish(self):
        self.inbox.offer(sup("completed", task_id="c", title="C"))
        self.inbox.offer(sup("approval_required", task_id="b", title="B",
                             summary="push the branch"))
        kinds = [u.kind for u in self.policy.decide(self.inbox)]
        self.assertEqual(kinds[0], "needs_input")

    def test_a_stale_finish_is_not_announced_unprompted(self):
        event = sup("completed", created_at="2020-01-01T00:00:00Z")
        self.inbox.offer(event)
        self.assertEqual(self.policy.decide(self.inbox), [])
        self.assertEqual(self.inbox.record(event.event_id).voice_decision,
                         "suppressed_stale")

    def test_each_event_is_decided_once(self):
        self.inbox.offer(sup("completed"))
        self.assertEqual(len(self.policy.decide(self.inbox)), 1)
        self.assertEqual(self.policy.decide(self.inbox), [])


class TheVoiceReportsBack(unittest.TestCase):
    def test_spoken_and_yielded_hooks_fire(self):
        outcomes = []

        async def speak(text):
            await asyncio.sleep(0.01)

        async def drive():
            c = VoiceCoordinator(speak=speak)
            c.on_spoken = lambda t: outcomes.append(("spoken", t))
            c.on_yielded = lambda t: outcomes.append(("yielded", t))
            c.enqueue("one", "completed", subject="a")
            await asyncio.sleep(0.05)
            c.enqueue("two", "completed", subject="b")
            await asyncio.sleep(0.002)
            c.interrupt_for_user()
            await asyncio.sleep(0.01)
            c.user_finished()
            await asyncio.sleep(0.05)
        asyncio.run(drive())
        self.assertIn(("spoken", "one"), outcomes)
        self.assertIn(("yielded", "two"), outcomes)


class TheProductIsWiredThisWay(unittest.TestCase):
    def test_the_card_path_never_touches_a_notification(self):
        source = Path("conduct.py").read_text()
        body = source[source.index("def project_state"):]
        body = body[:body.index("def offer_supervisory")]
        self.assertIn("project_card(state)", body)
        self.assertNotIn("notice_payload", body)
        self.assertNotIn("announce(", body)

    def test_activity_and_notify_no_longer_send_cards(self):
        source = Path("conduct.py").read_text()
        for name in ("def on_activity", "def on_notify", "def on_supersede"):
            body = source[source.index(name):]
            body = body[:body.index("\n    def ", 10)]
            self.assertNotIn("ui.send(notice=", body, name)

    def test_startup_renders_from_canonical_state(self):
        source = Path("conduct.py").read_text()
        self.assertIn("for state in conductor.subagent_states():", source)
        self.assertNotIn("for pending in notifications.store.list(unread_only=True)",
                         source)

    def test_the_manager_reads_the_inbox_and_acks_it(self):
        import inspect
        source = (inspect.getsource(GlobalConductor.handle_user_message)
                  + inspect.getsource(GlobalConductor._turn))
        self.assertIn("pending_for_manager", source)
        self.assertIn("ack_manager", source)
        self.assertIn("digest_for_manager",
                      inspect.getsource(GlobalConductor.global_context))


if __name__ == "__main__":
    unittest.main()
