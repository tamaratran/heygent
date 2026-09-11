"""Every finished utterance reaches the Boss, including a second one said
while work runs.

Measured, from real sessions under the old delegation protocol: work is
running, the user speaks again, the running work's result is appended into
the same model turn as the new transcript, and the frontend relays the
result while folding the new request into "Yep, got it" - nothing
downstream ever knows the second thing was said. The client no longer
waits for the model to hand anything over: the moment a user turn's
transcript is final, the client runs it itself.

Run with:  python3 -m unittest tests.test_second_utterance -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from conductor.observability import ObservabilityBus, ObservabilityEvent

try:                       # the audio stack is not installed everywhere
    import voice_agent
except Exception:          # pragma: no cover - exercised by skipping
    voice_agent = None


def setUpModule():
    if voice_agent is None:
        return
    import tempfile
    from pathlib import Path
    global _TRANSCRIPT_DIR, _REAL_TRANSCRIPT
    _TRANSCRIPT_DIR = tempfile.TemporaryDirectory()
    _REAL_TRANSCRIPT = voice_agent.TRANSCRIPT_LOG
    voice_agent.TRANSCRIPT_LOG = Path(_TRANSCRIPT_DIR.name) / "session.jsonl"


def tearDownModule():
    if voice_agent is None:
        return
    voice_agent.TRANSCRIPT_LOG = _REAL_TRANSCRIPT
    _TRANSCRIPT_DIR.cleanup()


class FakeSpeaker:
    speaking = False

    def flush(self) -> None:
        pass


class FakeWebSocket:
    def __init__(self, frames: list[dict]) -> None:
        import aiohttp
        import json as _json
        self.messages = [
            type("Msg", (), {"type": aiohttp.WSMsgType.TEXT,
                             "data": _json.dumps(frame)})()
            for frame in frames]
        self.closed = False

    def __aiter__(self):
        async def gen():
            for message in self.messages:
                yield message
        return gen()


SETTLE = 0.01


def user_said(text: str, turn_id: str = "turn_u") -> dict:
    """The user's words. There is no event for the end of them: the
    reader arms a timer, and the gap is the boundary."""
    return {"type": "session.input_transcript.delta", "delta": text}


def assistant_turn(text: str, turn_id: str) -> list[dict]:
    return [{"type": "session.output_transcript.delta", "delta": text}]


SECOND = "Additionally, I'm interrupting you to see if it shows up in the logs"


@unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
class DirectDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = ObservabilityBus()
        self.events: list[ObservabilityEvent] = []
        self.bus.subscribe(self.events.append)
        real_speaker = voice_agent.Speaker
        voice_agent.Speaker = FakeSpeaker
        self.addCleanup(setattr, voice_agent, "Speaker", real_speaker)
        self.agent = voice_agent.VoiceAgent(
            api_key="", ui=mock.Mock(), allow_write=False, bus=self.bus)
        self.agent.card = lambda *a, **k: None
        # What reached the working half, and what was said back.
        self.ran: list[tuple[str, str, str]] = []
        self.announced: list[str] = []

        async def fake_run(item_id, prompt, trace_id=""):
            self.ran.append((item_id, prompt, trace_id))

        async def fake_announce(text):
            self.announced.append(text)

        self.agent._run_claude = fake_run
        self.agent.announce = fake_announce

    def events_of(self, kind: str) -> list[ObservabilityEvent]:
        return [e for e in self.events if e.type == kind]

    def run_frames(self, frames: list[dict]) -> None:
        """Feed the reader and wait out the settle: a turn ends when its
        words stop arriving, not on an event."""
        self.agent.ASK_SETTLE_S = SETTLE
        self.agent.REPLY_SETTLE_S = SETTLE
        self.agent.POST_RELEASE_GRACE = 0.0   # no waiting for a real tail

        async def go():
            await self.agent._read_events(FakeWebSocket(frames))
            await asyncio.sleep(SETTLE * 8)

        asyncio.run(go())

    def speak(self, frames: list[dict], held_s: float = 1.5) -> str:
        """One utterance, end to end, in one loop: the key goes down, the
        words arrive, the key goes up, and the timer the release arms
        runs the work. Returns the trace the hold opened."""
        self.agent.ASK_SETTLE_S = SETTLE
        self.agent.REPLY_SETTLE_S = SETTLE
        self.agent.POST_RELEASE_GRACE = 0.0   # no waiting for a real tail

        async def go():
            await self.agent.set_holding(True)
            opened = self.agent.trace_id
            await self.agent._read_events(FakeWebSocket(frames))
            self.agent.held_at -= held_s
            await self.agent.set_holding(False)
            await asyncio.sleep(SETTLE * 8)
            return opened

        return asyncio.run(go())

    def first_request_is_running(self) -> None:
        self.speak([user_said("which PRs are in flight", "turn_u1")])
        self.assertEqual([r[1] for r in self.ran],
                         ["which PRs are in flight"])
        self.assertEqual(len(self.agent.in_flight), 1)

    def test_an_utterance_is_run_the_moment_its_transcript_is_final(self):
        trace = self.speak([user_said("which PRs are in flight")])
        self.assertEqual(len(self.ran), 1)
        _, prompt, trace_id = self.ran[0]
        self.assertEqual(prompt, "which PRs are in flight")
        self.assertEqual(trace_id, trace,
                         "the work keeps the utterance's trace")

    def test_a_second_utterance_mid_work_gets_its_own_work(self):
        self.first_request_is_running()
        second_trace = self.speak(                     # speaks over the work
            [user_said(SECOND, "turn_u2")]
            + assistant_turn("Yep, got it. Still pulling the PRs now.",
                             "turn_a2"))
        self.assertEqual(len(self.ran), 2, self.ran)
        item_id, prompt, trace_id = self.ran[1]
        self.assertEqual(prompt, SECOND)
        self.assertEqual(trace_id, second_trace,
                         "the second work keeps its own trace")
        self.assertIn(item_id, self.agent.in_flight)
        self.assertEqual(len(self.agent.in_flight), 2)

    def test_small_talk_reaches_the_boss_too(self):
        """Measured under the old protocol with nothing running: "Let's
        start a new voice agent" was answered by the frontend in four
        seconds with "Sure. What would you like that agent to do?" and
        never reached the Boss. What the user says goes to the Boss,
        spoken or typed - the frontend is ears and mouth."""
        self.speak([user_said("let's start a new voice agent")]
                   + assistant_turn("Sure. What would you like that "
                                    "agent to do?", "turn_a1"))
        self.assertEqual([text for _, text, _ in self.ran],
                         ["let's start a new voice agent"])

    def test_an_empty_transcript_starts_nothing(self):
        asyncio.run(self.agent.set_holding(True))
        self.run_frames([user_said("   ")])
        asyncio.run(self.agent.set_holding(False))
        self.assertEqual(self.ran, [])
        self.assertEqual(self.agent.in_flight, set())


@unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
class TheAnswerIsSpokenTest(unittest.TestCase):
    def setUp(self) -> None:
        from unittest import mock
        real_speaker = voice_agent.Speaker
        voice_agent.Speaker = FakeSpeaker
        self.addCleanup(setattr, voice_agent, "Speaker", real_speaker)
        self.agent = voice_agent.VoiceAgent(
            api_key="", ui=None, allow_write=False, bus=ObservabilityBus())
        self.agent.card = lambda *a, **k: None
        self.agent._work = mock.AsyncMock(return_value="It is in the logs.")
        self.agent.announce = mock.AsyncMock()

    def test_the_work_closes_and_its_answer_is_announced(self):
        self.agent.in_flight.add("work_1")
        asyncio.run(self.agent._run_claude("work_1",
                                           "see if it shows up in the logs"))
        self.agent.announce.assert_awaited_once_with("It is in the logs.")
        self.assertEqual(self.agent.in_flight, set())
        self.assertTrue(self.agent.awaiting_narration)


if __name__ == "__main__":
    unittest.main()
