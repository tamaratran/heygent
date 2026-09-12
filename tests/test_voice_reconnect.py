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
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
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


def run_loop(outcomes, stop_after=None, refusal="", refused_key=False):
    """Drive run() with canned connection outcomes. Each entry is what
    _one_connection returns; the loop must reconnect through the drops
    and stop only for the reasons that mean stop. Returns the attempts
    made, the sleeps between them, and the agent."""
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
    a.refusal = refusal
    a.refused_key = refused_key
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
    return calls, sleeps, a


class StayingUp(unittest.TestCase):
    def run_loop(self, outcomes, stop_after=None):
        calls, sleeps, _ = run_loop(outcomes, stop_after)
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


class ARefusalIsNotADrop(unittest.TestCase):
    """A key OpenAI rejected, an account with no credit: the socket opened,
    OpenAI said no, and heygent reconnected every half second for ever -
    "voice session dropped; reconnecting" - with nothing telling the user
    what to fix. A refusal is the same answer on every socket."""

    def test_no_credit_ends_the_reader_as_a_refusal(self):
        a = agent()
        ws = FakeWs([Frame(aiohttp.WSMsgType.TEXT, json.dumps({
            "type": "error", "error": {
                "type": "invalid_request_error",
                "code": "credit_balance_exhausted",
                "message": "You have no credits remaining. Add credits at "
                           "https://platform.openai.com/settings/"
                           "organization/billing/."}}))])
        with mock.patch.object(voice_agent, "application_log"):
            reason = asyncio.run(a._read_events(ws))
        self.assertEqual(reason, voice_agent.KEY_REFUSED)
        self.assertIn("no credit", a.refusal)
        self.assertIn("billing", a.refusal)
        self.assertFalse(a.refused_key, "the account, not the key, is short")

    def test_an_invalid_key_is_a_refusal_of_the_key_itself(self):
        a = agent()
        ws = FakeWs([Frame(aiohttp.WSMsgType.TEXT, json.dumps({
            "type": "error", "error": {"code": "invalid_api_key",
                                       "message": "Incorrect API key"}}))])
        with mock.patch.object(voice_agent, "application_log"):
            asyncio.run(a._read_events(ws))
        self.assertTrue(a.refused_key)
        self.assertIn("api-keys", a.refusal)

    def test_other_errors_are_still_reported_not_fatal(self):
        a = agent()
        ws = FakeWs([Frame(aiohttp.WSMsgType.TEXT, json.dumps({
            "type": "error", "error": {"code": "rate_limit_exceeded",
                                       "message": "slow down"}}))])
        with mock.patch.object(voice_agent, "application_log"):
            reason = asyncio.run(a._read_events(ws))
        self.assertEqual(reason, "socket ended")

    def test_a_401_handshake_is_a_refusal(self):
        a = agent()
        session = mock.Mock()
        info = mock.Mock(real_url="wss://x")
        session.ws_connect = mock.AsyncMock(
            side_effect=aiohttp.WSServerHandshakeError(
                info, (), status=401, message="Unauthorized"))
        reason = asyncio.run(a._one_connection(session, {}, 0))
        self.assertEqual(reason, voice_agent.KEY_REFUSED)
        self.assertTrue(a.refused_key)

    def test_the_loop_stops_tells_the_user_and_forgets_a_bad_key(self):
        told = []
        forgotten = []
        with mock.patch.object(voice_agent.dialogs, "tell", told.append), \
             mock.patch.object(voice_agent, "forget_api_key",
                               lambda: forgotten.append(1)), \
             mock.patch.object(voice_agent, "application_log"):
            calls, _, a = run_loop([voice_agent.KEY_REFUSED] * 5,
                                   stop_after=5,
                                   refusal="OpenAI rejected the API key",
                                   refused_key=True)
        self.assertEqual(calls, [0], "a refusal was retried")
        self.assertEqual(told, ["OpenAI rejected the API key"])
        self.assertEqual(forgotten, [1])
        a.ui.request_quit.assert_called_once()


class AskingForTheKey(unittest.TestCase):
    """Opened from Finder there is no terminal: the old prompt returned ""
    at once and the app quit with "OPENAI_API_KEY is empty - paste your
    key into .env" in a log file. Now a dialog asks, and a key OpenAI
    rejects is asked for again rather than saved and failed on later."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(voice_agent, "HERE", Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        mock.patch.object(voice_agent, "application_log").start()
        self.addCleanup(mock.patch.stopall)
        os.environ.pop("OPENAI_API_KEY", None)

    def test_without_a_terminal_a_dialog_asks_and_the_key_is_saved(self):
        answers = ["sk-good"]
        told = []
        with mock.patch.object(voice_agent.dialogs, "has_terminal",
                               return_value=False), \
             mock.patch.object(voice_agent.dialogs, "can_show",
                               return_value=True), \
             mock.patch.object(voice_agent.dialogs, "ask_secret",
                               lambda text: answers.pop(0)), \
             mock.patch.object(voice_agent.dialogs, "tell", told.append):
            key = voice_agent.ask_for_api_key(check=lambda k: None)
        self.assertEqual(key, "sk-good")
        self.assertIn("OPENAI_API_KEY=sk-good",
                      (Path(self.tmp.name) / ".env").read_text())
        self.assertEqual(told, [])

    def test_a_rejected_key_is_asked_for_again(self):
        answers = ["sk-typo", "sk-good"]
        told = []
        checks = {"sk-typo": "OpenAI rejected that key (HTTP 401).",
                  "sk-good": None}
        with mock.patch.object(voice_agent.dialogs, "has_terminal",
                               return_value=False), \
             mock.patch.object(voice_agent.dialogs, "can_show",
                               return_value=True), \
             mock.patch.object(voice_agent.dialogs, "ask_secret",
                               lambda text: answers.pop(0)), \
             mock.patch.object(voice_agent.dialogs, "tell", told.append):
            key = voice_agent.ask_for_api_key(check=checks.__getitem__)
        self.assertEqual(key, "sk-good")
        self.assertEqual(len(told), 1)
        self.assertIn("401", told[0])
        env = (Path(self.tmp.name) / ".env").read_text()
        self.assertNotIn("sk-typo", env)

    def test_cancelling_the_dialog_gives_up(self):
        with mock.patch.object(voice_agent.dialogs, "has_terminal",
                               return_value=False), \
             mock.patch.object(voice_agent.dialogs, "can_show",
                               return_value=True), \
             mock.patch.object(voice_agent.dialogs, "ask_secret",
                               lambda text: ""):
            self.assertEqual(voice_agent.ask_for_api_key(
                check=lambda k: None), "")
        self.assertFalse((Path(self.tmp.name) / ".env").exists())

    def test_forgetting_the_key_keeps_the_rest_of_env(self):
        (Path(self.tmp.name) / ".env").write_text(
            "OTHER=1\nOPENAI_API_KEY=sk-bad\n")
        voice_agent.forget_api_key()
        self.assertEqual((Path(self.tmp.name) / ".env").read_text(),
                         "OTHER=1\n")

    def test_check_api_key_reads_openais_verdict(self):
        def refuse(code):
            def opener(request, timeout):
                raise urllib.error.HTTPError(request.full_url, code, "no",
                                             {}, None)
            return opener

        self.assertIn("401", voice_agent.check_api_key("k", refuse(401)))
        self.assertIn("403", voice_agent.check_api_key("k", refuse(403)))
        self.assertIsNone(voice_agent.check_api_key("k", refuse(500)),
                          "a server error is not a verdict on the key")

        def offline(request, timeout):
            raise OSError("no network")
        self.assertIsNone(voice_agent.check_api_key("k", offline))


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
