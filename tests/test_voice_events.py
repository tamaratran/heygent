"""Voice-side observability: the microphone half of a trace.

The conductor half has always been traced. These tests cover the part that
happens before the Manager sees any text - the hold, the transcript, the
spoken reply - and the handoff that keeps both halves on one trace id.

Run with:  python3 -m unittest tests.test_voice_events -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from unittest import mock

from conductor.conductor import Conductor
from conductor.manager import FakeManagerBackend
from conductor.observability import (ObservabilityBus, ObservabilityEvent,
                                     current_trace, new_trace)
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager

try:                       # the audio stack is not installed everywhere
    import voice_agent
except Exception:          # pragma: no cover - exercised by skipping
    voice_agent = None


def setUpModule():
    """Never write into the real session transcript.

    These tests drive the same log() the live agent uses, so without this
    a test run appends fixture lines - "hey there", "my
    api_key=hunter2hunter2 ok" - into the user's actual conversation
    transcript, interleaved with what they really said.
    """
    import tempfile
    from pathlib import Path
    import voice_agent
    global _TRANSCRIPT_DIR, _REAL_TRANSCRIPT
    _TRANSCRIPT_DIR = tempfile.TemporaryDirectory()
    _REAL_TRANSCRIPT = voice_agent.TRANSCRIPT_LOG
    voice_agent.TRANSCRIPT_LOG = Path(_TRANSCRIPT_DIR.name) / "session.jsonl"


def tearDownModule():
    import voice_agent
    voice_agent.TRANSCRIPT_LOG = _REAL_TRANSCRIPT
    _TRANSCRIPT_DIR.cleanup()


class FakeSpeaker:
    """Enough of Speaker to drive the agent without an audio device."""

    def __init__(self) -> None:
        self.speaking = False
        self.flushed = 0

    def flush(self) -> None:
        self.flushed += 1


SETTLE = 0.01


def read_all(agent, frames, settle: float = SETTLE) -> None:
    """Feed the reader loop and wait out the settle: with no turn.done
    on the wire, a turn ends when its words stop arriving."""
    agent.ASK_SETTLE_S = settle
    agent.REPLY_SETTLE_S = settle
    agent.POST_RELEASE_GRACE = 0.0     # no waiting for a real tail

    async def go():
        await agent._read_events(FakeWebSocket(frames))
        await asyncio.sleep(settle * 8)

    asyncio.run(go())


class FakeWebSocket:
    """An async-iterable of Live API frames, as the reader loop sees them."""

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


@unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
class VoiceEventTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = ObservabilityBus()
        self.events: list[ObservabilityEvent] = []
        self.bus.subscribe(self.events.append)
        self.speaker = FakeSpeaker()
        real_speaker = voice_agent.Speaker
        voice_agent.Speaker = lambda: self.speaker
        self.addCleanup(setattr, voice_agent, "Speaker", real_speaker)
        # A stand-in overlay pipe: transcripts are drawn in the capsule
        # (boss.CAPSULE_WORDS), so the reader loop writes to it.
        self.agent = voice_agent.VoiceAgent(
            api_key="", ui=mock.Mock(), allow_write=False, bus=self.bus)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def find(self, event_type: str) -> ObservabilityEvent:
        match = [e for e in self.events if e.type == event_type]
        self.assertTrue(match, f"no {event_type} in {self.types()}")
        return match[0]

    def release(self, held_s: float = 1.5) -> None:
        """Let go of a key that was held long enough to have carried speech.

        An instant press-release is the shape of the phantom release the
        debounce in hotkey.py exists to swallow, and set_holding warns
        about it. Tests that mean an ordinary utterance should look like
        one.
        """
        self.agent.held_at -= held_s
        asyncio.run(self.agent.set_holding(False))

    # -- the hold -------------------------------------------------------
    def test_hold_opens_a_trace_and_times_the_release(self) -> None:
        asyncio.run(self.agent.set_holding(True))
        self.release()
        self.assertEqual(self.types(),
                         ["voice.listening_started", "voice.listening_stopped"])
        self.assertTrue(self.agent.trace_id.startswith("trace_"))
        # One utterance, one trace - both ends of the hold share it.
        self.assertEqual({e.trace_id for e in self.events},
                         {self.agent.trace_id})
        self.assertIsNotNone(self.find("voice.listening_stopped").duration_ms)

    def test_a_hold_too_short_to_be_a_finger_is_reported(self) -> None:
        """The phantom release, seen from the far end.

        hotkey.py should now swallow these, but if one ever gets through
        again it must not be silent: a closed microphone drops the user's
        words down the designed path, so this warning is the only trace
        the lost utterance leaves anywhere.
        """
        asyncio.run(self.agent.set_holding(True))
        with self.assertLogs("voice_conductor.hotkey", "WARNING") as caught:
            asyncio.run(self.agent.set_holding(False))   # released instantly
        self.assertIn("hotkey.hold_implausibly_short",
                      [r.__dict__.get("event") for r in caught.records])
        stopped = self.find("voice.listening_stopped")
        self.assertLess(stopped.duration_ms, self.agent.IMPLAUSIBLE_HOLD_MS)

    def test_a_normal_hold_is_not_reported_as_short(self) -> None:
        asyncio.run(self.agent.set_holding(True))
        with self.assertNoLogs("voice_conductor.hotkey", "WARNING"):
            self.release()

    def test_each_hold_starts_a_new_trace(self) -> None:
        asyncio.run(self.agent.set_holding(True))
        first = self.agent.trace_id
        self.release()
        asyncio.run(self.agent.set_holding(True))
        self.assertNotEqual(self.agent.trace_id, first)

    def test_repeated_state_is_not_re_emitted(self) -> None:
        asyncio.run(self.agent.set_holding(True))
        asyncio.run(self.agent.set_holding(True))
        self.assertEqual(self.types().count("voice.listening_started"), 1)

    def test_barge_in_only_when_it_interrupts_speech(self) -> None:
        asyncio.run(self.agent.set_holding(True))
        self.assertNotIn("voice.barge_in", self.types())
        self.release()
        self.speaker.speaking = True
        asyncio.run(self.agent.set_holding(True))
        self.assertIn("voice.barge_in", self.types())

    # -- the session ----------------------------------------------------
    def test_reader_loop_emits_the_spoken_turn(self) -> None:
        self.agent.reply_mode = "text"
        self.agent.card = lambda *a, **k: None
        read_all(self.agent, [
            {"type": "session.started", "session": {"id": "sess_1"}},
            {"type": "session.input_transcript.delta", "delta": "hey there"},
            {"type": "session.output_transcript.delta", "delta": "hello back"},
            {"type": "session.closed"},
        ])
        for expected in ("voice.session_started", "voice.transcript_added",
                         "voice.utterance_completed", "voice.reply_spoken",
                         "voice.session_closed"):
            self.assertIn(expected, self.types())
        self.assertEqual(self.find("voice.session_started")
                         .provider_session_id, "sess_1")
        self.assertEqual(self.find("voice.transcript_added").data["text"],
                         "hey there")
        self.assertEqual(self.find("voice.reply_spoken").data["text"],
                         "hello back")

    def test_errors_are_recorded(self) -> None:
        ws = FakeWebSocket([
            {"type": "error", "error": "rate limited"},
        ])
        self.agent.card = lambda *a, **k: None
        asyncio.run(self.agent._read_events(ws))
        self.assertEqual(self.find("voice.error").severity, "error")

    def test_the_work_keeps_the_trace_that_asked_for_it(self) -> None:
        """A second utterance mid-work must not re-parent the work."""
        captured: list[str] = []

        async def fake_run(item_id, prompt, trace_id=""):
            captured.append(trace_id)

        self.agent._run_claude = fake_run
        self.agent.card = lambda *a, **k: None
        self.agent.ASK_SETTLE_S = SETTLE
        self.agent.POST_RELEASE_GRACE = 0.0

        async def one_utterance():
            """Hold, speak, let go - in one loop, because the words are
            run by a timer the release arms."""
            await self.agent.set_holding(True)
            opened = self.agent.trace_id
            await self.agent._read_events(FakeWebSocket([
                {"type": "session.input_transcript.delta",
                 "delta": "do the thing"}]))
            self.agent.held_at -= 1.5
            await self.agent.set_holding(False)
            await asyncio.sleep(SETTLE * 8)
            return opened

        asked_under = asyncio.run(one_utterance())
        asyncio.run(self.agent.set_holding(True))     # the user speaks again
        self.assertNotEqual(self.agent.trace_id, asked_under)
        self.assertEqual(captured, [asked_under])

    def test_stage_directions_are_not_the_users_words(self) -> None:
        """The transcript writes "[breath]" for a breath. The sink keeps
        it (verbatim is verbatim); the words carried to the Boss do not."""
        read_all(self.agent, [
            {"type": "session.input_transcript.delta", "delta": "[breath"},
            {"type": "session.input_transcript.delta", "delta": "]Next thing"},
            {"type": "session.input_transcript.delta", "delta": " [Laughter] ok"},
        ])
        # Kept as cut: the mark is only whole once the fragments are joined.
        self.assertEqual([t for _, t in self.agent.spoken],
                         ["[breath", "]Next thing", " [Laughter] ok"])
        self.assertEqual(self.agent._verbatim_since_last_turn(),
                         "Next thing ok")
        self.assertEqual(voice_agent.without_noise("[breath ]Next thing"),
                         "Next thing")

    def test_transcripts_reach_the_sink_verbatim(self) -> None:
        """Local traces for the owner: what was heard is what is stored."""
        ws = FakeWebSocket([
            {"type": "session.input_transcript.delta",
             "delta": "my api_key=hunter2hunter2 ok"},
        ])
        asyncio.run(self.agent._read_events(ws))
        self.assertEqual(self.find("voice.transcript_added").data["text"],
                         "my api_key=hunter2hunter2 ok")


@unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
class SpeechDuringWorkTest(unittest.TestCase):
    """The second thing the user says, while the first is being worked on.

    From the log, 06:52, under the old delegation protocol: "Additionally,
    I'm interrupting you to see if it shows up in the logs", said mid-work,
    got "Yep, got it" and was never handed over; asked why, the model
    insisted it had responded. The client no longer waits for the model:
    it runs every finished utterance itself.
    """

    def setUp(self) -> None:
        import time
        self.time = time
        self.bus = ObservabilityBus()
        self.events: list[ObservabilityEvent] = []
        self.bus.subscribe(self.events.append)
        self.speaker = FakeSpeaker()
        real_speaker = voice_agent.Speaker
        voice_agent.Speaker = lambda: self.speaker
        self.addCleanup(setattr, voice_agent, "Speaker", real_speaker)
        self.agent = voice_agent.VoiceAgent(
            api_key="", ui=mock.Mock(), allow_write=False, bus=self.bus)
        self.agent.card = lambda *a, **k: None

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def test_an_utterance_goes_through_the_work_seam(self):
        """The dispatch itself is covered in test_second_utterance; this
        pins the seam it runs through: an utterance does _work, and its
        answer is spoken."""
        from unittest import mock
        self.agent._work = mock.AsyncMock(return_value="It is in the logs.")
        self.agent.announce = mock.AsyncMock()

        async def go():
            self.agent._start_work("see if it shows up in the logs",
                                   "trace_x")
            for _ in range(5):
                await asyncio.sleep(0)   # let the started task run through
        asyncio.run(go())
        self.agent._work.assert_awaited_once()
        self.assertIn("see if it shows up in the logs",
                      self.agent._work.await_args.args[0])
        self.agent.announce.assert_awaited_once_with("It is in the logs.")
        self.assertEqual(self.agent.in_flight, set())

    def test_the_manager_gets_the_users_own_words_too(self):
        """749931f added the verbatim backstop for exactly this case -
        the frontend forwarding "Yes, correct" - but put it inside the
        Claude-SDK body that conduct.py replaces, so the Manager never
        received it. It is attached above the seam now."""
        from unittest import mock
        import conduct
        from conductor.manager import ManagerTurn
        fake = mock.Mock()
        fake.bus = self.bus
        fake.handle_user_message = mock.AsyncMock(
            return_value=ManagerTurn(reply="Running them.", tool_calls=[]))
        agent = conduct.ConductorVoice(api_key="", ui=None, conductor=fake)
        agent.announce = mock.AsyncMock()
        agent.spoken.append((self.time.monotonic(),
                             "run the tests on PR twenty two"))
        asyncio.run(agent._run_claude("item_1", "Yes, correct", "trace_x"))
        sent = fake.handle_user_message.await_args.args[0]
        self.assertIn("run the tests on PR twenty two", sent)
        self.assertIn("Yes, correct", sent)
        agent.announce.assert_awaited_once_with("Running them.")

    def test_the_conductor_half_answers_through_the_manager(self):
        """conduct.py's override returns the Manager's reply, so the
        voice path speaks what the Manager answered."""
        from unittest import mock
        import conduct
        from conductor.manager import ManagerTurn
        fake = mock.Mock()
        fake.bus = self.bus
        fake.handle_user_message = mock.AsyncMock(
            return_value=ManagerTurn(reply="Two PRs are open.", tool_calls=[]))
        agent = conduct.ConductorVoice(api_key="", ui=None, conductor=fake)
        answer = asyncio.run(agent._work("check the logs", "trace_x"))
        self.assertEqual(answer, "Two PRs are open.")
        fake.handle_user_message.assert_awaited_once()


class TraceHandoffTest(unittest.TestCase):
    """The trace opened at the microphone must survive into the Manager."""

    def test_new_trace_adopts_an_existing_id(self) -> None:
        self.assertEqual(new_trace("trace_from_voice"), "trace_from_voice")
        self.assertEqual(current_trace(), "trace_from_voice")
        self.assertNotEqual(new_trace(), "trace_from_voice")

    def test_manager_turn_joins_the_voice_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = ObservabilityBus()
            events: list[ObservabilityEvent] = []
            bus.subscribe(events.append)
            conductor = Conductor(tmp, FakeCodingAgentRuntime(),
                                  workspaces=FakeWorkspaceManager(), bus=bus,
                                  manager=FakeManagerBackend())
            conductor.manager.next_actions = [
                ("create_task", {"title": "t", "goal": "g"})]
            asyncio.run(conductor.handle_user_message(
                "fix the login redirect", source="voice",
                trace_id="trace_from_voice"))
            self.assertTrue(events)
            self.assertEqual({e.trace_id for e in events},
                             {"trace_from_voice"})

    def test_a_typed_turn_still_mints_its_own_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = ObservabilityBus()
            events: list[ObservabilityEvent] = []
            bus.subscribe(events.append)
            conductor = Conductor(tmp, FakeCodingAgentRuntime(),
                                  workspaces=FakeWorkspaceManager(), bus=bus,
                                  manager=FakeManagerBackend())
            asyncio.run(conductor.handle_user_message("hello"))
            self.assertTrue(all(e.trace_id.startswith("trace_")
                                for e in events))


if __name__ == "__main__":
    unittest.main()
