"""One voice is not one queue.

2026-09-08, in the user's words: "the boss agent response and the working
agent response are somehow speaking at the same time".

VoiceCoordinator says in its own docstring that every user-facing
utterance serialises through it, and everything it queues does. But it is
not the only caller of the thing that actually speaks. VoiceAgent.announce
appends to the session's speakable channel, and two callers reach it:

    VoiceCoordinator._say          a queued notification - a worker
                                   finished, a worker needs you
    VoiceAgent._run_claude         the answer to what the user just asked

Nothing sat between them. Each waited for playback to drain before
returning, which serialises a caller against ITSELF and against nobody
else, so a Boss answer arriving while a worker update was being spoken
appended a second thing to the one channel and the user heard both.

The lock lives in announce, not in the coordinator, because announce is
the seam every speaker actually passes through.

Run with:  python3 -m unittest tests.test_one_voice_one_turn -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

try:
    import voice_agent
except Exception:                       # the audio stack is not installed
    voice_agent = None


class Recorder:
    """A websocket that records what reached the speakable channel, and a
    speaker that is audibly busy while an utterance is outstanding."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self.overlaps: list[str] = []
        self.busy = False

    async def send_json(self, frame: dict) -> None:
        if self.busy:
            self.overlaps.append(frame["content"])
        self.busy = True
        self.sent.append(frame["content"])


class FakeSpeaker:
    """Audible while the session owes an utterance, quiet after."""

    def __init__(self, wire: Recorder) -> None:
        self.wire = wire
        self.ticks = 0

    @property
    def speaking(self) -> bool:
        if not self.wire.busy:
            return False
        self.ticks += 1
        if self.ticks % 4 == 0:         # the utterance finishes
            self.wire.busy = False
            return False
        return True


@unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
class TheSpeakableChannelIsHeldByOneCaller(unittest.TestCase):
    def agent(self) -> "voice_agent.VoiceAgent":
        agent = voice_agent.VoiceAgent.__new__(voice_agent.VoiceAgent)
        agent.speech_lock = asyncio.Lock()
        agent.holding = False
        agent.wire = Recorder()
        agent.ws = agent.wire
        agent.speaker = FakeSpeaker(agent.wire)
        agent._next_id = lambda prefix: f"{prefix}_1"
        agent._emit = lambda *a, **k: None
        return agent

    def test_two_replies_do_not_reach_the_channel_at_once(self) -> None:
        """The Boss's answer and a worker update, together, the way they
        arrived on the night this was found."""
        agent = self.agent()

        async def both():
            await asyncio.gather(
                agent.announce("The login agent finished."),
                agent.announce("Two things. Your message did go to her."))
        asyncio.run(both())
        self.assertEqual(agent.wire.overlaps, [],
                         "a second utterance was appended mid-speech")
        self.assertEqual(len(agent.wire.sent), 2, "one of them was dropped")

    def test_the_second_caller_still_gets_said(self) -> None:
        """Serialising must not mean discarding: a notification held back
        while the Boss answers is spoken after it, not instead of it."""
        agent = self.agent()

        async def both():
            await asyncio.gather(agent.announce("first"),
                                 agent.announce("second"))
        asyncio.run(both())
        self.assertEqual(sorted(agent.wire.sent), ["first", "second"])

    def test_waiting_for_the_channel_is_a_line_in_the_log(self) -> None:
        agent = self.agent()
        events: list[str] = []
        agent._emit = lambda event, **k: events.append(event)

        async def both():
            await asyncio.gather(agent.announce("first"),
                                 agent.announce("second"))
        asyncio.run(both())
        self.assertIn("voice.speech_queued", events)

    def test_a_stream_that_never_drains_does_not_keep_the_channel(self) -> None:
        """The drain loop ends when the voice stops being audible, which
        it does. The ceiling is only so that a stream which does not
        cannot make the one speech path unusable for ever."""
        agent = self.agent()
        agent.speaker = mock.Mock(speaking=True)
        agent.SPEECH_CEILING_S = 0.05
        asyncio.run(agent.announce("something"))
        self.assertFalse(agent.speech_lock.locked())

    def test_a_closed_session_says_nothing(self) -> None:
        agent = self.agent()
        agent.ws.closed = True
        asyncio.run(agent.announce("nobody is listening"))
        self.assertEqual(agent.wire.sent, [])


if __name__ == "__main__":
    unittest.main()
