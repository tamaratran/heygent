"""The reply card retires itself once the answer has been said.

A finished answer is history the moment it is spoken; leaving it on screen
pushes the session rows further from the waveform for no benefit. It must
not disappear while the agent is still talking, though, and a new answer
has to cancel a pending retirement.

Run with:  python3 -m unittest tests.test_reply_lifetime -v
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path

import voice_agent


class ReplyLifetimeTest(unittest.TestCase):
    def source(self, name: str) -> str:
        text = Path(voice_agent.__file__).read_text()
        start = text.index(f"def {name}")
        end = text.find("\n    def ", start + 10)
        return text[start:end if end > 0 else len(text)]

    def test_a_finished_answer_starts_a_retirement_clock(self) -> None:
        loop = Path(voice_agent.__file__).read_text()
        self.assertIn("self.reply_expires_at = time.monotonic() "
                      "+ self.REPLY_LINGER", loop,
                      "a finished answer does not schedule its retirement")

    def test_streaming_cancels_a_pending_retirement(self) -> None:
        body = self.source("_stream_card")
        self.assertIn("self.reply_expires_at = 0.0", body,
                      "a still-talking agent would have its card pulled")

    def test_retirement_waits_for_the_audio_to_drain(self) -> None:
        loop = Path(voice_agent.__file__).read_text()
        block = loop[loop.index("Retire a finished reply"):]
        block = block[:block.index("ui.send(card=None)") + 20]
        self.assertIn("not self.speaker.speaking", block,
                      "the card could vanish mid-sentence")

    def test_the_linger_is_a_second_or_two(self) -> None:
        self.assertGreaterEqual(voice_agent.VoiceAgent.REPLY_LINGER, 1.0)
        self.assertLessEqual(voice_agent.VoiceAgent.REPLY_LINGER, 4.0)

    def test_retiring_clears_the_text_so_it_cannot_reappear(self) -> None:
        loop = Path(voice_agent.__file__).read_text()
        block = loop[loop.index("Retire a finished reply"):]
        block = block[:block.index("ui.send(card=None)") + 20]
        self.assertIn('self.reply_text = ""', block)
