"""The manager hears what the user said, whatever the frontend forwards.

From a real session, in the user's own transcript:

    23:11:20  you           "...I wanted you to run tests on PR 22"
    23:11:29  agent         "You want tests run on PR twenty two?"
    23:11:49  manager_turn  "Yes, correct"
    23:11:53  agent         "Okay, I'll take that to the agent."

The frontend answered the question itself and then forwarded only the
confirmation. The manager received "Yes, correct" with no idea what was
being confirmed, so it sent nothing - the debug log has no send event at
all - while the user had been told out loud that it was handled.

The frontend prompt now asks it not to do that. This is the backstop that
makes the prompt not have to be obeyed: the words come off the microphone
transcript, so they reach the manager either way.

Run with:  python3 -m unittest tests.test_verbatim_backstop -v
"""

from __future__ import annotations

import time
import unittest

import voice_agent
from voice_agent import VoiceAgent


def agent(*utterances, spoken_at=None):
    a = VoiceAgent.__new__(VoiceAgent)
    a.spoken = voice_agent.deque(maxlen=60)
    a.spoken_forwarded_at = 0.0
    now = time.monotonic()
    for i, text in enumerate(utterances):
        a.spoken.append((spoken_at if spoken_at is not None else now + i, text))
    return a


class TheWordsSurviveAThinTurn(unittest.TestCase):
    def test_a_bare_confirmation_still_carries_the_instruction(self):
        a = agent("I wanted you to", "run tests", "on PR 22", "Yes", ", correct")
        prompt = a._with_verbatim("Yes, correct")
        self.assertIn("run tests", prompt)
        self.assertIn("PR 22", prompt)

    def test_the_frontends_own_summary_is_kept_too(self):
        """It is the frontend's read of the request, and sometimes it is
        the better one. The backstop adds; it does not replace."""
        a = agent("run the tests on PR 22")
        prompt = a._with_verbatim("The user wants the test suite run")
        self.assertIn("The user wants the test suite run", prompt)
        self.assertIn("run the tests on PR 22", prompt)

    def test_words_already_forwarded_are_not_repeated(self):
        a = agent("run the tests on PR 22")
        prompt = a._with_verbatim("run the tests on PR 22")
        self.assertEqual(prompt.lower().count("run the tests on pr 22"), 1)

    def test_silence_leaves_the_prompt_alone(self):
        """A prompt with nothing spoken behind it - a retry, a
        follow-up the client raised itself - must not grow a block
        claiming the user said something."""
        a = agent()
        self.assertEqual(a._with_verbatim("do the thing"), "do the thing")


class ItDoesNotAccumulate(unittest.TestCase):
    def test_the_next_turn_carries_only_what_is_new(self):
        a = agent("run tests on PR 22")
        first = a._with_verbatim("Yes, correct")
        self.assertIn("run tests on PR 22", first)
        a.spoken.append((time.monotonic() + 100, "also check the linter"))
        second = a._with_verbatim("ok")
        self.assertIn("also check the linter", second)
        self.assertNotIn("run tests on PR 22", second,
                         "resent words the manager already acted on")

    def test_a_follow_up_with_nothing_new_still_has_context(self):
        """"do it" right after the last turn is a follow-up to what
        was already said, so sending nothing would be worse than sending
        the tail of it."""
        a = agent("run tests on PR 22")
        a._with_verbatim("Yes, correct")
        again = a._with_verbatim("and again")
        self.assertIn("PR 22", again)

    def test_it_is_bounded(self):
        a = agent(*[f"word{i}" for i in range(400)])
        self.assertLessEqual(len(a._with_verbatim("go")), 1400)

    def test_old_speech_is_not_dredged_up(self):
        """Something said three minutes ago is not context for this
        request."""
        a = agent("something from ages ago", spoken_at=time.monotonic() - 600)
        a.spoken_forwarded_at = time.monotonic() - 500
        prompt = a._with_verbatim("do the thing")
        self.assertIn("ages ago", prompt,
                      "with nothing newer, the tail is still the best we have")
        a2 = agent("ages ago", spoken_at=time.monotonic() - 600)
        a2.spoken.append((time.monotonic(), "fix the login bug"))
        self.assertIn("fix the login bug", a2._with_verbatim("ok"))


class ItIsWiredIn(unittest.TestCase):
    def test_speech_is_recorded_as_it_is_heard(self):
        from pathlib import Path
        source = Path(voice_agent.__file__).read_text()
        block = source[source.index(
            'elif kind == "session.input_transcript.delta"'):]
        block = block[:block.index("elif kind ==", 10)]
        self.assertIn("self.spoken.append", block,
                      "nothing records what the microphone heard")

    def test_every_work_item_goes_through_the_backstop(self):
        from pathlib import Path
        source = Path(voice_agent.__file__).read_text()
        run = source[source.index("async def _run_claude"):]
        run = run[:run.index("log(\"work\"")]
        self.assertIn("_with_verbatim", run)


if __name__ == "__main__":
    unittest.main()
