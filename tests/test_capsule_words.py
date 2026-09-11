"""Your words in the capsule: what shows, when, and where.

The overlay draws them from a timer that forgives nothing - a NameError in
the caption helpers once froze every later render - so the arithmetic is
pinned here without AppKit, the voice agent is shown to send the words, and
where AppKit exists one frame is drawn for real.

Run with:  python3 -m unittest tests.test_capsule_words -v
"""

from __future__ import annotations

import asyncio
import json
import unittest
from collections import deque
from unittest import mock

from conductor.capsule_words import (HIGHLIGHT_PAD, LANDING, TRAIL,
                                     TRAIL_FLOOR, CapsuleWords)

try:
    import aiohttp
    import voice_agent
    from voice_agent import VoiceAgent
    HAVE_VOICE = True
except Exception:                       # no audio stack in this interpreter
    HAVE_VOICE = False

try:
    import overlay
    from AppKit import NSImage, NSMakeRect, NSMakeSize
    HAVE_APPKIT = True
except Exception:                       # headless CI without pyobjc
    HAVE_APPKIT = False


def line() -> CapsuleWords:
    return CapsuleWords(lambda word: 7.0 * len(word), space=3.0)


def settle(words: CapsuleWords, live: bool = True, room: float = 400.0,
           frames: int = 200) -> None:
    for _ in range(frames):
        words.step(live, room)


class WhatShows(unittest.TestCase):
    def test_a_word_keeps_the_moment_it_landed(self):
        w = line()
        w.update("can", now=10.0)
        w.update("can you", now=11.0)
        self.assertEqual([word.landed for word in w.words], [10.0, 11.0])

    def test_a_word_still_being_transcribed_is_not_a_new_word(self):
        """Fragments split words; "pul" growing into "pull" is one arrival."""
        w = line()
        w.update("open the pul", now=1.0)
        w.update("open the pull", now=1.4)
        self.assertEqual((w.words[-1].text, w.words[-1].landed), ("pull", 1.0))

    def test_words_are_laid_out_end_to_end(self):
        w = line()
        w.update("can you", now=0.0)
        self.assertEqual([(word.x, word.width) for word in w.words],
                         [(0.0, 21.0), (24.0, 21.0)])
        self.assertEqual(w.width, 45.0)

    def test_an_empty_transcript_clears_everything(self):
        w = line()
        w.update("can you", 0.0)
        settle(w)
        w.update("", 5.0)
        self.assertEqual((w.words, w.hw, w.scroll, w.presence, w.reveal),
                         ([], 0.0, 0.0, 0.0, 0.0))

    def test_a_one_word_answer_shows(self):
        """Review of #197: "yes" never widens the capsule, and words used to
        fade in only as it widened, so a one-word answer stayed a dot."""
        w = line()
        w.update("Yes", now=0.0)
        settle(w)
        self.assertEqual(w.capsule_width(17.5, 73.0, 452.0), 73.0)
        self.assertGreater(w.reveal, 0.99)
        self.assertGreater(w.alpha(w.words[0], 1.0), 0.5)

    def test_a_new_word_fades_in_then_dims_to_the_trail(self):
        w = line()
        w.update("hello", now=0.0)
        word = w.words[0]
        self.assertEqual(w.alpha(word, 0.0), 0.0)
        self.assertGreater(w.alpha(word, LANDING), 0.9)
        self.assertAlmostEqual(w.alpha(word, TRAIL + 5.0), TRAIL_FLOOR)
        self.assertGreater(w.slide(word, 0.0), 0.0)
        self.assertEqual(w.slide(word, LANDING), 0.0)


class WhereItSits(unittest.TestCase):
    def test_the_capsule_hugs_the_words_up_to_its_widest(self):
        w = line()
        self.assertEqual(w.capsule_width(17.5, 73.0, 452.0), 73.0)
        w.update("can you", 0.0)
        self.assertEqual(w.capsule_width(17.5, 73.0, 452.0), 80.0)
        w.update(" ".join(["word"] * 60), 0.0)
        self.assertEqual(w.capsule_width(17.5, 73.0, 452.0), 452.0)

    def test_the_highlight_settles_on_the_newest_word(self):
        w = line()
        w.update("can you open", 0.0)
        settle(w)
        newest = w.words[-1]
        self.assertAlmostEqual(w.hx, newest.x - HIGHLIGHT_PAD, places=3)
        self.assertAlmostEqual(w.hw, newest.width + 2 * HIGHLIGHT_PAD, places=3)

    def test_the_highlight_fades_once_the_voice_stops(self):
        w = line()
        w.update("can you", 0.0)
        settle(w, live=True)
        self.assertGreater(w.presence, 0.99)
        settle(w, live=False)
        self.assertLess(w.presence, 0.01)

    def test_a_long_utterance_scrolls_to_keep_the_newest_word_in_view(self):
        w = line()
        room = 200.0
        w.update(" ".join(f"w{i:02d}" for i in range(30)), 0.0)
        settle(w, room=room)
        newest = w.words[-1]
        self.assertGreater(w.scroll, 0.0)
        self.assertLessEqual(newest.x + newest.width - w.scroll, room + 1e-6)
        self.assertLessEqual(w.scroll, w.width - room + 1e-6)

    def test_a_short_utterance_never_scrolls(self):
        w = line()
        w.update("can you", 0.0)
        settle(w, room=400.0)
        self.assertEqual(w.scroll, 0.0)

    def test_a_short_word_sits_in_the_middle_of_the_capsule(self):
        w = line()
        w.update("Yes", 0.0)
        self.assertAlmostEqual(w.start(73.0, 17.5) + w.width / 2, 73.0 / 2)
        w.update(" ".join(["word"] * 20), 0.0)
        self.assertEqual(w.start(452.0, 17.5), 17.5)

    def test_a_word_scrolled_off_the_left_is_gone(self):
        w = line()
        w.update(" ".join(f"w{i:02d}" for i in range(30)), 0.0)
        settle(w, room=200.0)
        self.assertEqual(w.alpha(w.words[0], 0.5), 0.0)
        self.assertGreater(w.alpha(w.words[-1], 0.5), 0.5)


@unittest.skipUnless(HAVE_VOICE, "aiohttp/sounddevice not available")
class TheVoiceSendsTheWords(unittest.TestCase):
    def agent(self):
        a = VoiceAgent.__new__(VoiceAgent)
        a.ui = mock.Mock()
        a.heard = ""
        a.spoken = deque(maxlen=60)
        a.asked_parts = []
        a._arm_ask = lambda: None
        a._emit = lambda *args, **kwargs: None
        return a

    def flags(self, words: bool, caption: bool):
        return mock.patch.multiple(voice_agent.boss, CAPSULE_WORDS=words,
                                   SHOW_CAPTION=caption)

    def test_the_transcript_so_far_reaches_the_capsule(self):
        fragments = ["Can you", " open the", " pull request"]
        frames = [_Frame(aiohttp.WSMsgType.TEXT, json.dumps(
            {"type": "session.input_transcript.delta", "delta": text}))
            for text in fragments]
        a = self.agent()
        with self.flags(words=True, caption=False), \
             mock.patch.object(voice_agent, "log", lambda *args, **kw: None), \
             mock.patch("builtins.print"):
            asyncio.run(a._read_events(_FakeWs(frames)))
        expected, so_far = [], ""
        for text in fragments:
            so_far = voice_agent.without_noise(
                voice_agent.join_fragments([so_far, text]))
            expected.append(so_far)
        sent = a.ui.send.call_args_list
        self.assertEqual([c.kwargs["words"] for c in sent if "words" in c.kwargs],
                         expected)
        self.assertIn("pull request", expected[-1])
        self.assertFalse(any("heard" in c.kwargs for c in sent),
                         "the caption card is off; it must not be fed")

    def test_with_the_setting_off_nothing_is_sent(self):
        frames = [_Frame(aiohttp.WSMsgType.TEXT, json.dumps(
            {"type": "session.input_transcript.delta", "delta": "hello"}))]
        a = self.agent()
        with self.flags(words=False, caption=False), \
             mock.patch.object(voice_agent, "log", lambda *args, **kw: None), \
             mock.patch("builtins.print"):
            asyncio.run(a._read_events(_FakeWs(frames)))
        self.assertFalse(any("words" in c.kwargs
                             for c in a.ui.send.call_args_list))

    def test_a_new_hold_starts_the_capsule_empty(self):
        a = self.agent()
        a.running, a.holding, a.was_holding = True, True, False
        a.speaker = mock.Mock(speaking=False)
        a.reply_expires_at = 0.0
        a.last_active = 0.0
        a.mic_level = 0.3
        a.heard = "the last thing you said"
        a.ui.state = "listening"

        async def once(_seconds):
            a.running = False

        with self.flags(words=True, caption=False), \
             mock.patch.object(voice_agent.asyncio, "sleep", once):
            asyncio.run(a._pump_ui())
        sent = a.ui.send.call_args_list
        self.assertEqual(a.heard, "")
        self.assertIn(mock.call(words=""), sent)
        listening = next(i for i, c in enumerate(sent)
                         if c.kwargs.get("state") == "listening")
        self.assertLess(sent.index(mock.call(words="")), listening,
                        "the old words would flash up before being cleared")


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class OneFrameDraws(unittest.TestCase):
    def test_a_word_has_a_width(self):
        self.assertGreater(overlay.measure_word("pull"), overlay.measure_word("a"))

    def test_words_in_the_capsule_draw_without_raising(self):
        view = overlay.PillView.alloc().initWithFrame_(
            NSMakeRect(0, 0, overlay.W_TEXT, overlay.H))
        view.state = "listening"
        view.voice = 0.6
        view.words.update("can you open the pull request", 0.0)
        for _ in range(30):
            view.words.step(True, overlay.W_TEXT - 2 * overlay.PAD_X)
        image = NSImage.alloc().initWithSize_(NSMakeSize(overlay.W_TEXT, overlay.H))
        with mock.patch.object(overlay, "CAPSULE_WORDS", True):
            image.lockFocus()
            try:
                view.drawRect_(view.bounds())                        # words
                overlay.draw_capsule_words(view, overlay.W_COMPACT, overlay.H)
                view.words.clear()
                overlay.draw_capsule_words(view, overlay.W_COMPACT, overlay.H)  # the dot
            finally:
                image.unlockFocus()


class _Frame:
    def __init__(self, type_, data=""):
        self.type = type_
        self.data = data


class _FakeWs:
    """A WebSocket that yields the given frames, then ends."""

    def __init__(self, frames):
        self.frames = list(frames)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)


if __name__ == "__main__":
    unittest.main()
