"""What the user says goes into the Boss's window the moment they say it.

Measured 2026-08-29 (conductor-64776.jsonl): a turn started at 08:07:37;
the next utterance came at 08:08:26 and was logged manager.turn_queued;
at 08:11 it was still not in the window, while the Boss ran tools for
the first one. The words sat in this process behind a turn lock.

Now they are typed at once. Claude Code keeps what arrives mid-turn as a
queued message in its input box - the user sees it land - and reads it
when it next looks up: into the running turn (one answer for both), or
as the turn after (its own answer). Which happened is read off the
transcript: a user line appears there when the message is read.

Run with:  python3 -m unittest tests.test_boss_streams_words -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.agent_events import AgentEvent
from conductor.manager import ManagerBackend, ManagerTurn
from conductor.pty_manager import PtyManagerBackend
from tests.test_boss_session import (FakeBridge, FakeConductor, FakeRuntime,
                                     fake_helper)


def user_line(text: str) -> AgentEvent:
    return AgentEvent(type="progress", summary=f"> {text}",
                      detail={"source": "user_message"})


class QueueingRuntime(FakeRuntime):
    """Claude Code as measured: a message sent mid-turn is queued in the
    input box, not read. The test plays the session - reads the queue,
    answers - the way the real one does."""

    def __init__(self):
        super().__init__()
        self.in_turn = False
        self.queued: list[str] = []
        self.sid = ""

    def _emit(self, event: AgentEvent) -> None:
        for handler in list(self.handlers.get(self.sid, [])):
            handler(event)

    async def send(self, session_id, message):
        self.sent.append((session_id, message))
        self.sid = session_id
        if self.in_turn:
            self.queued.append(message)      # lands in the box, unread
            return
        self.in_turn = True
        self._emit(user_line(message))

    def read_queued(self) -> None:
        """The Boss looks up mid-turn and takes what queued into it."""
        for message in self.queued:
            self._emit(user_line(message))
        self.queued = []

    def answer(self, text: str) -> None:
        """A turn end. Anything still queued starts the next turn."""
        self._emit(AgentEvent(type="completed", summary=text))
        self.in_turn = False
        if self.queued:
            self.in_turn = True
            self.read_queued()


class TheWordsGoInNow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.runtime = QueueingRuntime()
        self.conductor = FakeConductor()
        self.backend = PtyManagerBackend(
            self.runtime, self.home, self.home / "boss" / "tools.sock",
            python="/usr/bin/python3", repo_root="/repo", turn_timeout=2.0,
            helper=fake_helper(self.home), connect_timeout=0.2)
        self.backend.attach_bridge(FakeBridge())

    def tearDown(self):
        self.tmp.cleanup()

    def typed(self) -> list[str]:
        return [m for _, m in self.runtime.sent]

    def test_the_second_thing_said_is_typed_while_the_first_is_being_worked_on(self):
        async def scenario():
            first = asyncio.create_task(
                self.backend.handle("start the login fix", self.conductor))
            await asyncio.sleep(0.01)
            second = asyncio.create_task(
                self.backend.handle("also check billing", self.conductor))
            await asyncio.sleep(0.01)
            # Both are in the window; nothing has been answered yet.
            self.assertEqual(self.typed(),
                             ["start the login fix", "also check billing"])
            self.assertEqual(self.runtime.queued, ["also check billing"])
            self.assertTrue(self.backend.busy)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            self.runtime.read_queued()
            self.runtime.answer("Login fix started; billing is fine.")
            return await asyncio.gather(first, second)

        first, second = asyncio.run(scenario())
        self.assertFalse(self.backend.busy)
        # Read together, answered together: one answer, on the first.
        self.assertEqual(first.reply, "Login fix started; billing is fine.")
        self.assertFalse(first.folded)
        self.assertTrue(second.folded)
        self.assertEqual(second.reply, "")
        # And the record reads as the conversation the user had.
        kinds = [(e.type, e.payload.get("text", "")[:12])
                 for e in self.backend.store.events(self.backend.session.id)
                 if e.type in ("user_message", "boss_message")]
        self.assertEqual(kinds, [("user_message", "start the lo"),
                                 ("user_message", "also check b"),
                                 ("boss_message", "Login fix st")])

    def test_words_read_after_the_answer_get_an_answer_of_their_own(self):
        async def scenario():
            first = asyncio.create_task(
                self.backend.handle("start the login fix", self.conductor))
            await asyncio.sleep(0.01)
            second = asyncio.create_task(
                self.backend.handle("also check billing", self.conductor))
            await asyncio.sleep(0.01)
            # The Boss answers the first without looking up; Claude Code
            # then starts a turn for what queued.
            self.runtime.answer("Login fix started.")
            first_turn = await first
            self.assertFalse(second.done())
            self.runtime.answer("Billing checks out.")
            return first_turn, await second

        first, second = asyncio.run(scenario())
        self.assertEqual(first.reply, "Login fix started.")
        self.assertEqual(second.reply, "Billing checks out.")
        self.assertFalse(first.folded or second.folded)

    def test_an_answer_that_arrives_after_the_next_words_were_read_is_not_a_correction(self):
        """The Boss answered the first, then read the second: the first
        answer is final, and does not wait out the second turn."""
        async def scenario():
            first = asyncio.create_task(
                self.backend.handle("start the login fix", self.conductor))
            await asyncio.sleep(0.01)
            second = asyncio.create_task(
                self.backend.handle("also check billing", self.conductor))
            await asyncio.sleep(0.01)
            self.runtime.answer("Login fix started.")
            # The second turn runs long, chattering the whole time.
            for _ in range(6):
                await asyncio.sleep(0.02)
                self.runtime._emit(AgentEvent(type="progress", summary="looking"))
            self.assertTrue(first.done(),
                            "the first answer waited on the second turn")
            self.runtime.answer("Billing checks out.")
            return await first, await second

        first, second = asyncio.run(scenario())
        self.assertEqual(first.reply, "Login fix started.")
        self.assertEqual(second.reply, "Billing checks out.")

    def test_a_worker_update_is_typed_while_the_boss_is_mid_turn(self):
        """Not held until the user's turn ends - measured: three finishes
        held for three minutes behind one hung turn. Its own line in the
        transcript is not taken for the user's words, so the turn end
        still answers the user."""
        from tests.test_boss_updates import supervisory

        async def scenario():
            turn = asyncio.create_task(
                self.backend.handle("what's the weather in SF", self.conductor))
            await asyncio.sleep(0.01)
            self.backend.session.child_subagent_ids.append("sub_task_1")
            self.backend.deliver_supervisory(supervisory("task_1", summary="sunny, 61F"))
            await asyncio.sleep(0.01)
            self.assertIn("Your worker · Fix login (task_1) finished a turn: sunny, 61F",
                          self.typed()[-1], "the update waited for the turn to end")
            self.runtime.read_queued()                 # the Boss reads it in
            self.runtime.answer("Sunny and sixty-one in SF.")
            return await turn

        turn = asyncio.run(scenario())
        self.assertEqual(turn.reply, "Sunny and sixty-one in SF.")
        self.assertFalse(turn.folded)
        self.assertEqual(self.backend._pushes_open, 0)

    def test_words_the_session_never_wrote_do_not_shift_the_answers(self):
        """Measured 2026-08-30 08:47:15: an utterance was typed and
        confirmed sent, and Claude Code never wrote it. Matching lines by
        order then credited every reply to the utterance before the one
        it answered, and the newest turn hung open. The lost words are
        typed a second time and answered on their own turn."""
        async def scenario():
            first = asyncio.create_task(
                self.backend.handle("check the weather in SF", self.conductor))
            await asyncio.sleep(0.01)
            second = asyncio.create_task(
                self.backend.handle("I just said check the weather", self.conductor))
            await asyncio.sleep(0.01)
            third = asyncio.create_task(
                self.backend.handle("and are we on a PR branch", self.conductor))
            await asyncio.sleep(0.01)
            # The session lets the second one go, and reads the third.
            self.runtime.queued.remove("I just said check the weather")
            self.runtime.answer("Weather is on its way.")
            first_turn = await first
            await asyncio.sleep(0.01)          # the retype goes back in
            self.assertFalse(second.done(), "the lost one was dropped")
            self.assertIn("I just said check the weather", self.runtime.queued)
            self.assertFalse(third.done())
            self.runtime.answer("Yes, on master.")
            third_turn = await third
            self.runtime.answer("You did; it's coming.")
            return first_turn, await second, third_turn

        first, second, third = asyncio.run(scenario())
        self.assertEqual(first.reply, "Weather is on its way.")
        self.assertEqual(second.reply, "You did; it's coming.")
        self.assertFalse(second.folded)
        self.assertEqual(third.reply, "Yes, on master.")

    def test_a_line_typed_in_the_window_is_still_the_users_when_nothing_is_out(self):
        async def scenario():
            turn = asyncio.create_task(self.backend.handle("hi", self.conductor))
            await asyncio.sleep(0.01)
            self.runtime.answer("Hello.")
            await turn
        asyncio.run(scenario())
        self.runtime._emit(user_line("what's open?"))
        typed = [e for e in self.backend.store.events(self.backend.session.id)
                 if e.type == "user_message" and e.payload.get("source") == "typed"]
        self.assertEqual([e.payload["text"] for e in typed], ["what's open?"])


try:
    import conduct
    from conductor.global_conductor import GlobalConductor
    from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager
except Exception:          # pragma: no cover - exercised by skipping
    conduct = None


class _FoldingBackend(ManagerBackend):
    async def handle(self, text: str, conductor) -> ManagerTurn:
        return ManagerTurn(reply="", folded=True)


@unittest.skipIf(conduct is None, "conduct needs the audio stack")
class AFoldedAnswerIsNotSpoken(unittest.TestCase):
    """The answer was given on the earlier item. This one closes without
    a word: nothing is announced, and the work item is done."""

    def setUp(self) -> None:
        import voice_agent
        self.tmp = tempfile.TemporaryDirectory()
        real_transcript = voice_agent.TRANSCRIPT_LOG
        voice_agent.TRANSCRIPT_LOG = Path(self.tmp.name) / "session.jsonl"
        self.addCleanup(setattr, voice_agent, "TRANSCRIPT_LOG", real_transcript)
        real_speaker = voice_agent.Speaker
        voice_agent.Speaker = lambda: type("S", (), {"speaking": False,
                                                     "flush": lambda s: None})()
        self.addCleanup(setattr, voice_agent, "Speaker", real_speaker)
        self.conductor = GlobalConductor(
            home=Path(self.tmp.name) / "home",
            runtime=FakeCodingAgentRuntime(), manager=_FoldingBackend(),
            workspace_factory=lambda project: FakeWorkspaceManager())
        self.voice = conduct.ConductorVoice("", None, self.conductor)
        self.announced: list[str] = []

        async def announce(text):
            self.announced.append(text)

        self.voice.announce = announce

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_it_closes_its_work_item_in_silence(self):
        async def run():
            self.voice.in_flight.add("item_1")
            await self.voice._run_claude("item_1", "also check billing")
        asyncio.run(run())
        self.assertEqual(self.announced, [])
        self.assertNotIn("item_1", self.voice.in_flight)


if __name__ == "__main__":
    unittest.main()
