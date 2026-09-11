"""The user's words reach the Boss with ordinary spacing.

Measured, from the conductor log: the words forwarded to the Boss read
"Yeah , Um ." and "Got it . Um .Anyway , Uh .let's go", with "[tong ue
click ]" and "[mouth noise ]" in among them. Two causes. The transcriber
cuts an utterance into fragments that carry their own leading space or
mark - " my", ", Uh", ".formatting", "]It" - and the microphone side
joined them with spaces, so every comma and period became a word of its
own. And the transcriber's own final transcript has the space on the
wrong side of a mark ("in fact, Uh .formatting") and brackets sounds
outside the fixed list of them ("[tongue click ]").

Run with:  python3 -m unittest tests.test_transcript_spacing -v
"""

from __future__ import annotations

import time
import unittest

import voice_agent
from voice_agent import (VoiceAgent, join_fragments, tidy_spacing,
                         without_noise)

# One utterance, exactly as the fragments arrived (conductor log, 2026-08-29).
FRAGMENTS = [" in", " fact", ", Uh", ".formatting", " going", " on", ". Like",
             " between", " words", " and", " punctuation"]


def agent(*fragments):
    a = VoiceAgent.__new__(VoiceAgent)
    a.spoken = voice_agent.deque(maxlen=60)
    a.spoken_forwarded_at = 0.0
    now = time.monotonic()
    for i, text in enumerate(fragments):
        a.spoken.append((now + i, text))
    return a


class FragmentsJoinAsTheyWereCut(unittest.TestCase):
    def test_a_fragment_brings_its_own_space_or_mark(self):
        self.assertEqual(join_fragments(FRAGMENTS),
                         "in fact, Uh. formatting going on. Like between words"
                         " and punctuation".replace("in fact", " in fact"))

    def test_marks_do_not_become_words(self):
        self.assertEqual(without_noise(join_fragments(["Yeah", ", Um", "."])),
                         "Yeah, Um.")

    def test_whole_utterances_still_get_a_space_between_them(self):
        self.assertEqual(join_fragments(["run the tests", "on PR 22"]),
                         "run the tests on PR 22")

    def test_a_mark_glued_to_the_next_word_is_a_cut_word(self):
        self.assertEqual(join_fragments([", Uh", ".formatting"]),
                         ", Uh. formatting")

    def test_a_tag_cut_in_two_is_still_a_tag(self):
        joined = join_fragments(["Yeah, Um", ".", " [tong", "ue click ]let me"])
        self.assertEqual(without_noise(joined), "Yeah, Um. let me")
        self.assertEqual(without_noise(join_fragments(["[mouth noise", "]It"])),
                         "It")


class TheTranscribersOwnSpacingIsTidied(unittest.TestCase):
    def test_the_space_moves_to_the_right_side_of_the_mark(self):
        self.assertEqual(
            tidy_spacing("Yeah, Um .I want to like , Um. let me think."),
            "Yeah, Um. I want to like, Um. let me think.")

    def test_only_a_space_that_is_there_is_moved(self):
        """Nothing is invented: a period inside a token stays inside it."""
        self.assertEqual(tidy_spacing("version 3.5 on github.com e.g. now"),
                         "version 3.5 on github.com e.g. now")

    def test_any_bracketed_sound_is_a_stage_direction(self):
        self.assertEqual(without_noise("[mouth noise ]It looks like"),
                         "It looks like")
        self.assertEqual(without_noise("[tongue click ]let me"), "let me")
        self.assertEqual(without_noise("[breath]"), "")


class TheBossReadsOrdinaryText(unittest.TestCase):
    def test_the_verbatim_block_has_no_space_before_its_punctuation(self):
        a = agent(*FRAGMENTS)
        prompt = a._with_verbatim("fix the spacing")
        self.assertIn("in fact, Uh. formatting going on. Like between words"
                      " and punctuation", prompt)
        self.assertNotIn(" ,", prompt)
        self.assertNotIn(" .", prompt)

    def test_a_confirmation_cut_from_its_comma_reads_as_one(self):
        a = agent("Yes", ", correct")
        self.assertEqual(a._verbatim_since_last_turn(), "Yes, correct")


if __name__ == "__main__":
    unittest.main()
