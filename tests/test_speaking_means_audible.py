"""Speaking means a voice is coming out, not that the buffer has bytes.

Measured on the wire on 2026-08-28: the Live session streams 100 ms of
output audio ten times a second for the whole life of the session, and
between replies that audio is digital silence. So "the playback buffer
is not empty" was true from the first frame to the last, and everything
built on Speaker.speaking read wrong:

- voice.barge_in fired on every single hold;
- the reply card, which retires once the voice has stopped, never did;
- "[latency] first word" measured the first *silent* delta after release,
  and reported 2-40 ms on every reply.

Run with:  python3 -m unittest tests.test_speaking_means_audible -v
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import unittest
from unittest import mock

import numpy as np

from conductor.observability import ObservabilityBus, ObservabilityEvent

try:
    import voice_agent
except Exception:                       # the audio stack is not installed
    voice_agent = None

SILENCE = b"\x00" * (2400 * 2)          # one 100 ms delta of nothing


def voice_block(n: int = 2400) -> bytes:
    """A block with a speaking-level tone in it (~ -20 dBFS)."""
    t = np.arange(n) / 24_000
    return (np.sin(2 * np.pi * 220 * t) * 3000).astype(np.int16).tobytes()


class _Msg:
    def __init__(self, frame: dict) -> None:
        import aiohttp
        self.type = aiohttp.WSMsgType.TEXT
        self.data = json.dumps(frame)


class FakeWebSocket:
    def __init__(self, frames: list[dict]) -> None:
        self.messages = [_Msg(f) for f in frames]
        self.closed = False

    def __aiter__(self):
        async def gen():
            for message in self.messages:
                yield message
        return gen()


@unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
class SpeakingMeansAudible(unittest.TestCase):
    def speaker(self) -> "voice_agent.Speaker":
        with mock.patch.object(voice_agent.Speaker, "_open", lambda self: None):
            speaker = voice_agent.Speaker()
        speaker.last_drain = time.monotonic()       # the device is draining
        return speaker

    def test_the_sessions_silence_is_not_speech(self) -> None:
        speaker = self.speaker()
        for _ in range(5):
            speaker.feed(SILENCE)
        self.assertGreater(len(speaker.buffer), 0)   # queued, as always
        self.assertFalse(speaker.speaking)

    def test_a_voice_in_the_buffer_is(self) -> None:
        speaker = self.speaker()
        speaker.feed(SILENCE)
        speaker.feed(voice_block())
        self.assertTrue(speaker.speaking)

    def test_it_stops_once_the_voice_has_been_played_out(self) -> None:
        speaker = self.speaker()
        speaker.feed(voice_block())
        speaker.last_audible = time.monotonic() - 1.0   # a second ago
        speaker.feed(SILENCE)                            # the stream goes on
        self.assertFalse(speaker.speaking)

    def test_flush_ends_it_at_once(self) -> None:
        speaker = self.speaker()
        speaker.feed(voice_block())
        speaker.flush()
        self.assertFalse(speaker.speaking)


@unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
class TheFirstWordIsTheFirstAudibleDelta(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[ObservabilityEvent] = []
        bus = ObservabilityBus()
        bus.subscribe(self.events.append)
        real = voice_agent.Speaker
        voice_agent.Speaker = lambda: mock.Mock(speaking=False)
        self.addCleanup(setattr, voice_agent, "Speaker", real)
        self.agent = voice_agent.VoiceAgent(
            api_key="", ui=None, allow_write=False, bus=bus)
        self.agent.reply_mode = "text"
        self.agent.card = lambda *a, **k: None
        self.agent.released_at = time.monotonic() - 0.5
        self.agent.awaiting_first_audio = True

    def deltas(self, *blocks: bytes) -> FakeWebSocket:
        return FakeWebSocket([
            {"type": "session.output_audio.delta",
             "audio": base64.b64encode(block).decode()}
            for block in blocks])

    def test_silence_after_release_is_not_the_reply(self) -> None:
        asyncio.run(self.agent._read_events(self.deltas(SILENCE, SILENCE)))
        self.assertNotIn("voice.reply_started",
                         [e.type for e in self.events])
        self.assertTrue(self.agent.awaiting_first_audio)   # still waiting

    def test_the_first_audible_delta_is(self) -> None:
        asyncio.run(self.agent._read_events(
            self.deltas(SILENCE, SILENCE, voice_block(), voice_block())))
        started = [e for e in self.events if e.type == "voice.reply_started"]
        self.assertEqual(len(started), 1)
        self.assertGreater(started[0].duration_ms, 400)   # not 2 ms
        self.assertFalse(self.agent.awaiting_first_audio)


if __name__ == "__main__":
    unittest.main()
