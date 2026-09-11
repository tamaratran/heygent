"""The Boss is the gate between workers and the user's ears.

Workers report to the Boss. A finish used to be read out mechanically
the moment a worker's turn ended - "the X agent finished" for a turn that
ended mid-work, three times for three workers - before anyone judged it.
Now the mechanical announcer says only what blocks the user, and a finish
reaches them when the Boss calls tell_user, in the Boss's words.

Run with:  python3 -m unittest tests.test_tell_user -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import boss
from conductor.global_conductor import GlobalConductor, MANAGER_TOOLS
from conductor.manager import FakeManagerBackend, ManagerTurn
from conductor.observability import ObservabilityBus
from conductor.supervisor_inbox import (SpeechPolicy, SupervisorInbox,
                                        SupervisoryEvent)
from conductor.testing import FakeCodingAgentRuntime


class AnsweringBoss:
    """A visible Boss mid-turn: its reply is what the user hears."""

    def __init__(self) -> None:
        self._in_voice_turn = False
        self.told = ""

    async def handle(self, text, conductor):
        self._in_voice_turn = True
        try:
            self.told = await conductor.handle_action(
                "tell_user", {"text": "Two PRs are closed."})
        finally:
            self._in_voice_turn = False
        return ManagerTurn(reply="Two PRs are closed.")


class TellUserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.bus = ObservabilityBus()
        self.bus.subscribe(self.events.append)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def conductor(self, manager) -> GlobalConductor:
        return GlobalConductor(home=Path(self.tmp.name) / "home",
                               runtime=FakeCodingAgentRuntime(),
                               manager=manager, bus=self.bus, search_roots=[])

    def said(self) -> list[str]:
        return [e.data["text"] for e in self.events if e.type == "boss.tell_user"]

    def test_it_is_a_manager_tool(self) -> None:
        self.assertIn("tell_user", MANAGER_TOOLS)

    def test_between_turns_it_is_spoken(self) -> None:
        """A worker update was pushed into the Boss's window; it judged it
        worth hearing. Nothing else is speaking, so it goes out."""
        gc = self.conductor(FakeManagerBackend())
        result = asyncio.run(gc.handle_action(
            "tell_user", {"text": "The PR agent is done: nine and sixteen are closed."}))
        self.assertEqual(result, "said")
        self.assertEqual(self.said(),
                         ["The PR agent is done: nine and sixteen are closed."])

    def test_during_a_voice_turn_it_refuses_rather_than_speaking_twice(self):
        manager = AnsweringBoss()
        gc = self.conductor(manager)
        turn = asyncio.run(gc.handle_user_message("close them", source="voice"))
        self.assertEqual(turn.reply, "Two PRs are closed.")
        self.assertIn("put this in your reply", manager.told)
        self.assertEqual(self.said(), [])

    def test_nothing_is_nothing(self) -> None:
        gc = self.conductor(FakeManagerBackend())
        self.assertEqual(asyncio.run(gc.handle_action("tell_user", {"text": "  "})),
                         "nothing to say")
        self.assertEqual(self.said(), [])


class TheAnnouncerIsLimitedToWhatBlocksTheUser(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = SupervisorInbox(Path(self.tmp.name) / "inbox.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def offer(self, type_: str, task: str, summary: str) -> None:
        self.inbox.offer(SupervisoryEvent(
            event_id=f"evt_{type_}_{task}", task_id=task, subagent_id=f"sub_{task}",
            type=type_, summary=summary,
            requires_action=type_ in ("approval_required", "input_required"),
            trace_id="", source_event_id="", task_title=task.title(),
            project_name="posely"))

    def test_attention_mode_speaks_blockers_and_leaves_finishes_to_the_boss(self):
        self.offer("completed", "login", "All tests pass.")
        self.offer("approval_required", "billing", "run the migration")
        said = SpeechPolicy(mode="attention").decide(self.inbox)
        self.assertEqual([u.kind for u in said], ["needs_input"])
        self.assertTrue(all("migration" in u.text for u in said))

    def test_the_default_is_attention(self) -> None:
        self.assertEqual(boss.NOTIFY_MODE, "attention")
        self.assertIn("attention", SpeechPolicy().spoken_types)


class TheProductIsWiredThisWay(unittest.TestCase):
    def test_the_boss_can_be_heard_between_turns(self) -> None:
        source = Path("conduct.py").read_text()
        body = source[source.index("def boss_speaks"):]
        body = body[:body.index("\n    voice.on_spoken")]
        self.assertIn('"boss.tell_user"', body)
        self.assertIn('announce(text, "boss"', body)
        self.assertIn("conductor.bus.subscribe(boss_speaks)", source)


if __name__ == "__main__":
    unittest.main()
