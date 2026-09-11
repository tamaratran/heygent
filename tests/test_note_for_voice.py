"""What the Boss tells the voice on the side reaches it, silently.

The Boss's spoken answer was all the voice ever got. Everything the Boss
learned on the way - which file, which task id, which PR was the draft -
was either read aloud or lost, and "which one was the draft?" meant
another round trip for a fact the Boss had ten seconds earlier.

note_for_voice is a Boss tool. Notes are gathered for the turn, ride
back with it as ManagerTurn.commentary, and the voice side hands them
to the voice model on the session's commentary channel before the
spoken answer - never spoken, never typed into any window.

Run with:  python3 -m unittest tests.test_note_for_voice -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor.global_conductor import GlobalConductor, MANAGER_TOOLS
from conductor.manager import ManagerTurn
from conductor.observability import ObservabilityBus
from conductor.testing import FakeCodingAgentRuntime


class NotingBoss:
    """A Manager that answers and leaves a note for the voice."""

    def __init__(self, notes):
        self.notes = notes

    async def handle(self, text, conductor):
        for note in self.notes:
            await conductor.handle_action("note_for_voice", {"text": note})
        return ManagerTurn(reply="Two PRs are open.")


class TheNoteRidesBackWithTheTurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        bus = ObservabilityBus()
        bus.subscribe(self.events.append)
        self.bus = bus

    def tearDown(self):
        self.tmp.cleanup()

    def conductor(self, notes):
        return GlobalConductor(home=Path(self.tmp.name) / "home",
                               runtime=FakeCodingAgentRuntime(),
                               manager=NotingBoss(notes), bus=self.bus,
                               search_roots=[])

    def test_it_is_a_manager_tool(self):
        self.assertIn("note_for_voice", MANAGER_TOOLS)

    def test_notes_reach_the_turn_and_not_the_reply(self):
        gc = self.conductor(["PRs: #51 draft, #52 open", "repo tamaratran/gptree"])
        turn = asyncio.run(gc.handle_user_message("check my PRs", source="voice"))
        self.assertEqual(turn.reply, "Two PRs are open.")
        self.assertEqual(turn.commentary,
                         "PRs: #51 draft, #52 open\nrepo tamaratran/gptree")
        self.assertIn("boss.note_for_voice", [e.type for e in self.events])

    def test_a_turn_without_notes_has_none(self):
        gc = self.conductor([])
        turn = asyncio.run(gc.handle_user_message("hi", source="voice"))
        self.assertEqual(turn.commentary, "")

    def test_notes_do_not_leak_into_the_next_turn(self):
        gc = self.conductor(["first turn's note"])
        asyncio.run(gc.handle_user_message("one", source="voice"))
        gc.manager = NotingBoss([])
        turn = asyncio.run(gc.handle_user_message("two", source="voice"))
        self.assertEqual(turn.commentary, "")

    def test_an_empty_note_is_nothing(self):
        gc = self.conductor(["   "])
        turn = asyncio.run(gc.handle_user_message("x", source="voice"))
        self.assertEqual(turn.commentary, "")

    def test_a_note_between_turns_goes_out_now_not_with_the_next_turn(self):
        """The Boss replying to a pushed worker update, with no voice
        turn open. Measured 08:44:43: the PR 59 summary went into a note
        like this, the next turn started clean, and the user heard
        nothing until they said hello again."""
        gc = self.conductor([])
        asyncio.run(gc.handle_action("note_for_voice",
                                     {"text": "PR #81 is a design doc, no code"}))
        notes = [e for e in self.events if e.type == "boss.note_for_voice"]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].data.get("outside_turn"))
        self.assertEqual(notes[0].data["text"], "PR #81 is a design doc, no code")
        turn = asyncio.run(gc.handle_user_message("hi", source="voice"))
        self.assertEqual(turn.commentary, "", "delivered once, not again")


try:
    import voice_agent
except Exception:          # pragma: no cover - needs the audio stack
    voice_agent = None


@unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
class TheVoiceHandsItOverSilently(unittest.TestCase):
    def setUp(self):
        class FakeSpeaker:
            speaking = False
            def flush(self): pass
        real = voice_agent.Speaker
        voice_agent.Speaker = FakeSpeaker
        self.addCleanup(setattr, voice_agent, "Speaker", real)
        self.agent = voice_agent.VoiceAgent(api_key="", ui=None,
                                            allow_write=False,
                                            bus=ObservabilityBus())
        self.sent = []
        for name in ("_session_commentary", "announce"):
            async def record(*args, _name=name):
                self.sent.append((_name, *args))
            setattr(self.agent, name, record)

        async def work(prompt, trace_id=""):
            return "Two PRs are open."
        self.agent._work = work

    def test_the_note_precedes_the_spoken_answer(self):
        self.agent.in_flight.add("item_1")
        self.agent.pending_commentary = "PRs: #51 draft"
        asyncio.run(self.agent._run_claude("item_1", "check my PRs"))
        self.assertEqual(self.sent, [("_session_commentary", "PRs: #51 draft"),
                                     ("announce", "Two PRs are open.")])
        self.assertEqual(self.agent.pending_commentary, "", "a note is for one turn")

    def test_no_note_no_commentary(self):
        self.agent.in_flight.add("item_1")
        asyncio.run(self.agent._run_claude("item_1", "do it"))
        self.assertEqual([s[0] for s in self.sent], ["announce"])

    def wire(self, coro_of):
        frames = []

        class Ws:
            closed = False

            async def send_json(self, frame):
                frames.append(frame)

        agent = voice_agent.VoiceAgent(api_key="", ui=None, allow_write=False,
                                       bus=ObservabilityBus())
        agent.ws = Ws()
        asyncio.run(coro_of(agent))
        return frames

    def test_a_note_goes_on_the_thinking_channel(self):
        """The one thing that makes it silent. The session has two
        channels and their names read backwards: commentary is SPOKEN
        (the model paraphrases it aloud), thinking is not. A note is for
        the voice to know, never to say, so it is thinking."""
        frames = self.wire(lambda a: a._session_commentary("repo x"))
        self.assertEqual(frames[0]["type"], "session.thinking.append")
        self.assertEqual(frames[0]["content"], "repo x")
        self.assertIsNone(frames[0]["delegation_id"])
        self.assertNotIn("channel", frames[0])

    def test_an_answer_goes_on_the_channel_that_is_spoken(self):
        frames = self.wire(lambda a: a.announce("Two PRs are open."))
        self.assertEqual(frames[0]["type"], "session.commentary.append")
        self.assertEqual(frames[0]["content"], "Two PRs are open.")


class TheConductorVoiceCarriesIt(unittest.TestCase):
    def test_work_takes_the_commentary_off_the_turn(self):
        try:
            import conduct
        except Exception:
            self.skipTest("conduct needs the audio stack")
        fake = mock.Mock()
        fake.bus = ObservabilityBus()
        fake.manager_busy = False
        fake.handle_user_message = mock.AsyncMock(
            return_value=ManagerTurn(reply="Two PRs are open.",
                                     commentary="PRs: #51 draft"))
        agent = conduct.ConductorVoice(api_key="", ui=None, conductor=fake)
        answer = asyncio.run(agent._work("check my PRs", "trace_x"))
        self.assertEqual(answer, "Two PRs are open.")
        self.assertEqual(agent.pending_commentary, "PRs: #51 draft")


try:
    import conduct
    from conductor.observability import ObservabilityEvent
except Exception:          # pragma: no cover - needs the audio stack
    conduct = None


@unittest.skipIf(conduct is None, "conduct needs the audio stack")
class TheVoiceTakesANoteBetweenTurns(unittest.TestCase):
    def setUp(self):
        class FakeSpeaker:
            speaking = False
            def flush(self): pass
        real = voice_agent.Speaker
        voice_agent.Speaker = FakeSpeaker
        self.addCleanup(setattr, voice_agent, "Speaker", real)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        real_log = voice_agent.TRANSCRIPT_LOG
        voice_agent.TRANSCRIPT_LOG = Path(self.tmp.name) / "session.jsonl"
        self.addCleanup(setattr, voice_agent, "TRANSCRIPT_LOG", real_log)
        conductor = GlobalConductor(home=Path(self.tmp.name) / "home",
                                    runtime=FakeCodingAgentRuntime(),
                                    manager=NotingBoss([]), search_roots=[])
        self.voice = conduct.ConductorVoice("", None, conductor)
        self.handed: list[str] = []

        async def commentary(text):
            self.handed.append(text)
        self.voice._session_commentary = commentary

    def test_only_a_note_made_between_turns_is_handed_over_here(self):
        handler = conduct.notes_between_turns(self.voice)

        async def run():
            handler(ObservabilityEvent(
                type="boss.note_for_voice", component="manager",
                data={"text": "PR #81 is a design doc", "outside_turn": True}))
            handler(ObservabilityEvent(
                type="boss.note_for_voice", component="manager",
                data={"text": "rides with the turn"}))
            handler(ObservabilityEvent(type="boss.tell_user", component="manager",
                                       data={"text": "spoken elsewhere"}))
            await asyncio.sleep(0)
        asyncio.run(run())
        self.assertEqual(self.handed, ["PR #81 is a design doc"])


if __name__ == "__main__":
    unittest.main()
