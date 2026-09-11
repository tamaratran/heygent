"""The live session's own protocol, as the wire actually speaks it.

The voice was built against a preview of the live API, on a model and
an endpoint that a GA key cannot reach at all (measured: the socket
refuses the handshake with 400, and the preview model is a 404). This
holds the shape of the GA protocol in place - the endpoint, the first
message, the event names, and the one thing worth breaking a build
over: which of the two context channels is spoken.

Run with:  python3 -m unittest tests.test_live_protocol -v
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

import boss

try:
    import voice_agent
except Exception:                    # pragma: no cover - needs the audio stack
    voice_agent = None

SOURCE = Path(__file__).resolve().parent.parent / "voice_agent.py"


class TheHandshake(unittest.TestCase):
    def test_the_socket_is_the_sessions_endpoint(self):
        self.assertEqual(voice_agent.LIVE_WS,
                         "wss://api.openai.com/v1/live/sessions")

    def test_the_model_is_not_a_query_parameter(self):
        """It goes in session.start. A ?model= is the preview's shape,
        and the GA endpoint refuses it."""
        text = SOURCE.read_text()
        self.assertNotIn("?model=", text)
        self.assertIn("ws_connect(LIVE_WS", text)

    def test_the_model_is_the_generally_available_one(self):
        self.assertEqual(boss.LIVE_MODEL, "gpt-live-1")

    def test_nothing_carries_the_preview_header_any_more(self):
        text = SOURCE.read_text()
        for gone in ("ALPHA_HEADER", "OpenAI-Alpha"):
            self.assertNotIn(gone, text, f"{gone} is still here")

    def test_the_first_message_starts_the_session(self):
        text = SOURCE.read_text()
        self.assertIn('"type": "session.start"', text)
        self.assertNotIn('"type": "session.update"', text)
        start = text[text.index('"type": "session.start"'):][:400]
        for field in ('"model": MODEL', '"instructions"', '"audio"'):
            self.assertIn(field, start)


@unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
class TheTwoChannelsAreNotSwapped(unittest.TestCase):
    """commentary is spoken; thinking is not. The names read backwards,
    and getting them the wrong way round reads the user's private notes
    out loud."""

    def wire(self, call):
        frames = []

        class Ws:
            closed = False

            async def send_json(self, frame):
                frames.append(frame)

        agent = voice_agent.VoiceAgent.__new__(voice_agent.VoiceAgent)
        agent.ws = Ws()
        agent.speech_lock = asyncio.Lock()
        agent.holding = False
        agent.speaker = type("S", (), {"speaking": False})()
        agent._emit = lambda *a, **k: None
        agent.SPEECH_CEILING_S = 0.01
        asyncio.run(call(agent))
        return frames

    def test_what_the_user_hears_goes_on_commentary(self):
        frames = self.wire(lambda a: a.announce("Two PRs are open."))
        self.assertEqual(frames[0]["type"], "session.commentary.append")
        self.assertEqual(frames[0]["content"], "Two PRs are open.")

    def test_what_only_the_voice_knows_goes_on_thinking(self):
        frames = self.wire(lambda a: a._session_commentary("PR #12 is the draft"))
        self.assertEqual(frames[0]["type"], "session.thinking.append")
        self.assertEqual(frames[0]["content"], "PR #12 is the draft")

    def test_neither_carries_the_old_channel_field(self):
        for call in (lambda a: a.announce("said"),
                     lambda a: a._session_commentary("noted")):
            frame = self.wire(call)[0]
            self.assertNotIn("channel", frame)
            self.assertIsInstance(frame["content"], str)
            self.assertIn("delegation_id", frame)


class TheEventsAreTheOnesTheSessionSends(unittest.TestCase):
    """Measured against a real session (2026-09-10): these are the names
    that arrive, and there is no turn.done among them."""

    def setUp(self):
        self.text = SOURCE.read_text()

    def test_the_reader_knows_the_session_prefixed_names(self):
        for event in ("session.output_audio.delta",
                      "session.input_transcript.delta",
                      "session.output_transcript.delta",
                      "session.delegation.created",
                      "session.started", "session.closed"):
            self.assertIn(f'kind == "{event}"', self.text, event)

    def test_the_preview_names_are_gone(self):
        for gone in ('kind == "turn.done"', 'kind == "turn.created"',
                     'kind == "turn.delta"',
                     'kind == "input_transcript.added"',
                     'kind == "output_audio.delta"'):
            self.assertNotIn(gone, self.text, gone)

    def test_the_microphone_is_gated_by_muting(self):
        self.assertIn('MIC_RESUME = "session.input_audio.unmute"', self.text)
        self.assertIn('MIC_PAUSE = "session.input_audio.mute"', self.text)
        self.assertIn('"type": "session.input_audio.append"', self.text)


@unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
class ATurnEndsWhenItsWordsStop(unittest.TestCase):
    """There is no event for the end of a turn. The reader waits for a
    gap - and, for the user's words, for the key to be up long enough
    that the transcript has caught up with the finger."""

    def agent(self):
        a = voice_agent.VoiceAgent.__new__(voice_agent.VoiceAgent)
        a.asked_parts = []
        a.ask_settle = None
        a.reply_settle = None
        a.holding = False
        a.released_at = 0.0
        a.trace_id = "trace_x"
        a.request = ""
        a.ASK_SETTLE_S = 0.01
        a.POST_RELEASE_GRACE = 0.0
        a._emit = lambda *args, **kw: None
        a.started = []
        a._start_work = lambda text, trace: a.started.append(text)
        return a

    def settle(self, agent, wait: float = 0.2):
        async def go():
            agent._arm_ask()
            await asyncio.sleep(wait)
        asyncio.run(go())

    def test_the_words_are_run_once_they_stop(self):
        a = self.agent()
        a.asked_parts = ["Hey", ", how many", " PRs"]
        self.settle(a)
        self.assertEqual(a.started, ["Hey, how many PRs"])
        self.assertEqual(a.asked_parts, [], "the words are not run twice")

    def test_nothing_runs_while_the_key_is_still_down(self):
        a = self.agent()
        a.holding = True
        a.asked_parts = ["still talking"]
        self.settle(a)
        self.assertEqual(a.started, [])

    def test_the_tail_of_a_sentence_is_not_a_second_utterance(self):
        """Measured: the transcript trailed the release by 0.9s, and
        firing on the gap alone cut one question into two - the second
        of them the word "repo"."""
        a = self.agent()
        a.POST_RELEASE_GRACE = 0.3
        a.released_at = __import__("time").monotonic()
        a.asked_parts = ["how many PRs are open on my"]

        async def go():
            a._arm_ask()
            await asyncio.sleep(0.05)
            a.asked_parts.append(" repo")      # the tail lands late
            await asyncio.sleep(0.6)

        asyncio.run(go())
        self.assertEqual(a.started, ["how many PRs are open on my repo"])

    def test_silence_alone_runs_nothing(self):
        a = self.agent()
        self.settle(a)
        self.assertEqual(a.started, [])


if __name__ == "__main__":
    unittest.main()
