"""The socket ending is not the session ending.

Three silent deaths in one evening, each logged as a clean `app.stopped`
with zero errors - one of them mid-sentence, right after the agent had
successfully resumed a task for the user. The cause was two lines: the
event reader broke on any non-text WebSocket frame, and the main loop
exited the moment the reader finished. A ping, a close frame, or a
dropped connection therefore quit the whole product, with the conductor
and every worker still perfectly healthy underneath it.

Now the socket is reopened with the same instructions, and the only ways
out are a stop the user asked for or the server closing the session on
purpose.

Run with:  python3 -m unittest tests.test_voice_reconnect -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import aiohttp

import voice_agent
from voice_agent import VoiceAgent


class Frame:
    def __init__(self, type_, data=""):
        self.type = type_
        self.data = data


class FakeWs:
    """A WebSocket that yields the given frames, then ends."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def send_json(self, payload):
        self.sent.append(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True


def agent():
    a = VoiceAgent.__new__(VoiceAgent)
    a.ws = None
    a.stopping = False
    a.running = True
    a.holding = False
    a.muted_turn = False
    a.awaiting_first_audio = False
    a.released_at = 0.0
    a.trace_id = ""
    a.event_id = 0
    a._emit = lambda *args, **kwargs: None
    a._next_id = lambda prefix: f"{prefix}_1"
    return a


class ReadingToTheEnd(unittest.TestCase):
    def test_a_non_text_frame_names_itself(self):
        """"break" told nobody why three sessions died."""
        a = agent()
        ws = FakeWs([Frame(aiohttp.WSMsgType.CLOSE)])
        reason = asyncio.run(a._read_events(ws))
        self.assertIn("CLOSE", reason)

    def test_the_server_closing_on_purpose_is_distinguished(self):
        a = agent()
        ws = FakeWs([Frame(aiohttp.WSMsgType.TEXT,
                           '{"type": "session.closed"}')])
        self.assertEqual(asyncio.run(a._read_events(ws)), "closed_by_server")

    def test_a_socket_that_just_ends_says_so(self):
        a = agent()
        self.assertEqual(asyncio.run(a._read_events(FakeWs([]))),
                         "socket ended")


class StayingUp(unittest.TestCase):
    def run_loop(self, outcomes, stop_after=None):
        """Drive run() with canned connection outcomes. Each entry is
        what _one_connection returns; the loop must reconnect through
        the drops and stop only for the reasons that mean stop."""
        a = agent()
        a.api_key = "k"
        a.speaker = mock.Mock()
        a.ui = mock.Mock()
        a.ui.proc.stdout = None
        a._open_mic = lambda loop: None
        a._pump_mic = mock.AsyncMock()
        a._pump_ui = mock.AsyncMock()
        a._watch_audio = mock.AsyncMock()
        a.mic = None
        calls = []
        script = list(outcomes)

        async def one(session, headers, attempt):
            calls.append(attempt)
            if stop_after is not None and len(calls) >= stop_after:
                a.stopping = True
            return script.pop(0) if script else "socket ended"

        a._one_connection = one
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with mock.patch.object(voice_agent.asyncio, "sleep", fake_sleep), \
             mock.patch.object(voice_agent.sd, "query_devices",
                               return_value={"name": "x"}), \
             mock.patch.object(voice_agent, "close_quietly", lambda s: None), \
             mock.patch.object(voice_agent.aiohttp, "ClientSession") as cs:
            cs.return_value.__aenter__ = mock.AsyncMock(return_value=None)
            cs.return_value.__aexit__ = mock.AsyncMock(return_value=False)
            asyncio.run(a.run())
        return calls, sleeps

    def test_a_dropped_socket_is_reconnected(self):
        """The failure that quit the product three times."""
        calls, _ = self.run_loop(
            ["non-text frame: CLOSE", "non-text frame: PING", "socket ended"],
            stop_after=4)
        self.assertGreaterEqual(len(calls), 4,
                                "gave up after the first drop")

    def test_the_server_closing_on_purpose_ends_the_loop(self):
        calls, _ = self.run_loop(["closed_by_server"])
        self.assertEqual(calls, [0])

    def test_a_requested_stop_ends_the_loop(self):
        calls, _ = self.run_loop(["socket ended"] * 10, stop_after=2)
        self.assertEqual(len(calls), 2)

    def test_retries_back_off_but_start_fast(self):
        """Most drops are one bad frame. Every second of silence after
        one is a second the user is talking to nobody, so the first
        retry is quick; a genuinely dead network is not hammered."""
        _, sleeps = self.run_loop(["socket ended"] * 8, stop_after=8)
        self.assertLessEqual(sleeps[0], 1.0)
        self.assertEqual(sleeps, sorted(sleeps))
        self.assertLessEqual(max(sleeps), VoiceAgent.RECONNECT_CAP)

    def test_audio_is_not_torn_down_between_connections(self):
        """The microphone does not care which socket its blocks go down.
        Reopening it per connection would drop the user's words during
        every reconnect."""
        a = agent()
        opened = []
        a.api_key = "k"
        a.speaker = mock.Mock()
        a.ui = mock.Mock()
        a.ui.proc.stdout = None
        a._open_mic = lambda loop: opened.append(1)
        a._pump_mic = mock.AsyncMock()
        a._pump_ui = mock.AsyncMock()
        a._watch_audio = mock.AsyncMock()
        a.mic = None
        n = {"calls": 0}

        async def one(session, headers, attempt):
            n["calls"] += 1
            if n["calls"] >= 3:
                a.stopping = True
            return "socket ended"

        a._one_connection = one
        with mock.patch.object(voice_agent.asyncio, "sleep", mock.AsyncMock()), \
             mock.patch.object(voice_agent.sd, "query_devices",
                               return_value={"name": "x"}), \
             mock.patch.object(voice_agent, "close_quietly", lambda s: None), \
             mock.patch.object(voice_agent.aiohttp, "ClientSession") as cs:
            cs.return_value.__aenter__ = mock.AsyncMock(return_value=None)
            cs.return_value.__aexit__ = mock.AsyncMock(return_value=False)
            asyncio.run(a.run())
        self.assertEqual(opened, [1], "the microphone was reopened per socket")


class TheMicrophoneOutlivesTheSocket(unittest.TestCase):
    def test_the_pump_keeps_running_through_a_reconnect_gap(self):
        """_pump_mic is created once for the life of the agent, and it
        used to RETURN the moment the socket was gone - so after the first
        reconnect the microphone was dead, silently, for ever. Caught by
        reading the code before shipping, not by a user."""
        a = agent()
        a.mic_q = asyncio.Queue()
        sent = []

        async def drive():
            # gap: no socket at all
            a.ws = None
            a.mic_q.put_nowait(b"\x00\x00")
            pump = asyncio.create_task(a._pump_mic())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # reconnected
            ws = mock.Mock()
            ws.closed = False
            ws.send_json = mock.AsyncMock(side_effect=lambda p: sent.append(p))
            a.ws = ws
            a.mic_q.put_nowait(b"\x01\x01")
            await asyncio.sleep(0.05)
            a.running = False
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
        asyncio.run(drive())
        self.assertEqual(len(sent), 1,
                         "the pump died in the gap and never sent again")


class CloseMeansStop(unittest.TestCase):
    def test_close_prevents_a_reconnect(self):
        """Ctrl-C used to end the socket; now ending the socket reconnects,
        so close() has to say it means it."""
        a = agent()
        a.ws = None
        asyncio.run(a.close())
        self.assertTrue(a.stopping)


if __name__ == "__main__":
    unittest.main()
