"""A follow-up reaches the worker in the user's words, not the manager's.

What workers actually received at 23:59 tonight, after the manager prompt
had been tightened to "you are a switchboard here, not an author":

    task_16f95123  'Did we fix the area where it said "remote control..."'
                                                            <- verbatim
    task_20cb6ebd  'Additional read-only question from the user: did we
                    ever fix...'                            <- wrapped
    task_7fbead35  "Here's the actual instruction, read-only: run the gh
                    commands yourself (don't just tell the user what to
                    run) and report..."                     <- wrapped, and
                                                               an instruction
                                                               the user never
                                                               gave

Half the time. A rule a model follows half the time is not a rule, so it
lives in code at send_to_task, the one point every follow-up passes.

Also here: the backstop that was supposed to make the frontend's
forwarding not matter was added to VoiceAgent._run_claude, which the
product's ConductorVoice OVERRIDES. It never ran. The wiring test that
"passed" was reading the base class.

Run with:  python3 -m unittest tests.test_follow_ups_are_verbatim -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from conductor.global_conductor import GlobalConductor, hold_to_users_words

SAID = "run the gh commands and tell me which PRs are in flight"


class HoldingTheManagerToTheUsersWords(unittest.TestCase):
    def test_the_users_own_sentence_passes_untouched(self):
        sent, overruled = hold_to_users_words(SAID, SAID)
        self.assertEqual(sent, SAID)
        self.assertEqual(overruled, "")

    def test_a_quoted_part_of_it_passes(self):
        """One utterance can be about two tasks; the manager routes a part
        by quoting it, and a quote is still the user's words."""
        sent, overruled = hold_to_users_words("tell me which PRs are in flight",
                                              SAID)
        self.assertEqual(sent, "tell me which PRs are in flight")
        self.assertEqual(overruled, "")

    def test_framing_is_stripped_back_to_what_was_said(self):
        """The real one from tonight."""
        wrote = ("Here's the actual instruction, read-only: run the gh "
                 "commands yourself (don't just tell the user what to run) "
                 "and report which PRs are in flight")
        sent, overruled = hold_to_users_words(wrote, SAID)
        self.assertEqual(sent, SAID)
        self.assertEqual(overruled, wrote)
        self.assertNotIn("don't just tell the user", sent,
                         "an instruction the user never gave got through")

    def test_a_preface_is_stripped(self):
        wrote = "Additional read-only question from the user: " + SAID
        sent, _ = hold_to_users_words(wrote, SAID)
        self.assertEqual(sent, SAID)

    def test_punctuation_and_case_do_not_count_as_rewriting(self):
        """Transcription noise the manager tidied is not a paraphrase."""
        sent, overruled = hold_to_users_words(
            "Run the gh commands and tell me which PRs are in flight.", SAID)
        self.assertEqual(overruled, "")

    def test_nothing_spoken_leaves_the_message_alone(self):
        """A typed turn, or one the manager raised itself, has no
        utterance to hold anything to."""
        sent, overruled = hold_to_users_words("carry on with the plan", "")
        self.assertEqual(sent, "carry on with the plan")
        self.assertEqual(overruled, "")


class ItHappensOnTheRealPath(unittest.TestCase):
    def conductor(self):
        gc = GlobalConductor.__new__(GlobalConductor)
        gc._utterance = ""
        gc.manager = mock.Mock()
        gc.list_tasks = lambda: []
        gc.list_projects = lambda: []
        gc._emit = mock.Mock()
        inner = mock.Mock()
        inner.send_to_task = mock.AsyncMock()
        task = mock.Mock(id="task_a", project_id="p")
        gc._find_task = lambda tid, pid=None: (inner, task)
        gc._touch = lambda *a: None
        return gc, inner

    def test_send_to_task_sends_what_the_user_said(self):
        gc, inner = self.conductor()
        gc._utterance = SAID
        asyncio.run(gc.send_to_task("task_a", "Here's the instruction: " + SAID))
        inner.send_to_task.assert_awaited_once_with("task_a", SAID)

    def test_an_overrule_is_visible_in_the_log(self):
        """Silently swapping the manager's text would hide how often the
        prompt is being ignored, which is the number that decides whether
        this rule stays."""
        gc, _ = self.conductor()
        gc._utterance = SAID
        asyncio.run(gc.send_to_task("task_a", "The user wants PR status."))
        events = [c.args[0] for c in gc._emit.call_args_list]
        self.assertIn("manager.follow_up_rewritten", events)

    def test_the_utterance_lasts_exactly_one_turn(self):
        """Left behind, it would rewrite the NEXT turn's follow-up to
        whatever was said before."""
        gc, _ = self.conductor()

        async def handle(text, conductor):
            self.assertEqual(conductor._utterance, SAID)
            return mock.Mock(tool_calls=[], reply="ok")
        gc.manager.handle = handle
        with mock.patch("conductor.global_conductor.new_trace"):
            asyncio.run(gc.handle_user_message("x", utterance=SAID))
        self.assertEqual(gc._utterance, "")

    def test_the_utterance_is_cleared_even_when_the_turn_fails(self):
        gc, _ = self.conductor()

        async def handle(text, conductor):
            raise RuntimeError("boom")
        gc.manager.handle = handle
        with mock.patch("conductor.global_conductor.new_trace"):
            with self.assertRaises(RuntimeError):
                asyncio.run(gc.handle_user_message("x", utterance=SAID))
        self.assertEqual(gc._utterance, "")


    def test_overlapping_turns_are_held_to_everything_still_being_worked_on(self):
        """Turns overlap: a visible Boss takes the next thing said mid-turn
        and may act on both at once. A follow-up sent then is held to
        everything the user has said that is still being worked on, in
        order; once the first turn is answered, only the newer words
        remain; nothing is left behind after the last."""
        gc, inner = self.conductor()
        second_started = asyncio.Event()
        first_ended = asyncio.Event()

        async def handle(text, conductor):
            if text == "a":
                await second_started.wait()      # the other turn is open too
                await conductor.send_to_task("task_a", "Here is what to do.")
                return mock.Mock(tool_calls=[], reply="ok")
            second_started.set()
            await first_ended.wait()             # the first has been answered
            await conductor.send_to_task("task_a", "And then this.")
            return mock.Mock(tool_calls=[], reply="ok")
        gc.manager.handle = handle

        async def both():
            first = asyncio.create_task(
                gc.handle_user_message("a", utterance="first thing"))
            second = asyncio.create_task(
                gc.handle_user_message("b", utterance="second thing"))
            await first
            first_ended.set()
            await second
        with mock.patch("conductor.global_conductor.new_trace"):
            asyncio.run(both())
        self.assertEqual(
            [c.args for c in inner.send_to_task.await_args_list],
            [("task_a", "first thing second thing"), ("task_a", "second thing")])
        self.assertEqual(gc._utterance, "")


try:                       # the audio stack is not installed everywhere
    import conduct
except Exception:          # pragma: no cover - exercised by skipping
    conduct = None


@unittest.skipIf(conduct is None, "conduct needs the audio stack")
class TheProductPathIsWired(unittest.TestCase):
    def test_conductor_voice_passes_the_utterance_through(self):
        """An earlier wiring test covered the base class while the product
        ran none of it. This one drives the class the product uses, from
        the work item the frontend starts to the conductor call."""
        import time
        import voice_agent

        real_speaker = voice_agent.Speaker
        voice_agent.Speaker = lambda: mock.Mock(speaking=False)
        self.addCleanup(setattr, voice_agent, "Speaker", real_speaker)
        conductor = mock.Mock()
        conductor.bus = mock.Mock()
        conductor.manager_busy = False
        conductor.handle_user_message = mock.AsyncMock(
            return_value=mock.Mock(tool_calls=[], reply="ok"))
        voice = conduct.ConductorVoice("", None, conductor)
        voice.announce = mock.AsyncMock()
        voice.spoken.append((time.monotonic(), SAID))

        asyncio.run(voice._run_claude("item_1", "Yes, correct"))

        kwargs = conductor.handle_user_message.await_args.kwargs
        self.assertEqual(kwargs["utterance"], SAID)
        self.assertIn(SAID, conductor.handle_user_message.await_args.args[0])

    def test_the_words_are_attached_once_and_handed_down_once(self):
        """Two fixes for the same bug met in a rebase: the backstop moved
        above the seam into the base _run_claude, and the conductor's
        _work also called it. The second call found nothing new since the
        first and answered "" - an empty utterance, which switched
        send_to_task's enforcement off for every turn. The seam reads what
        was attached; it never asks again."""
        import inspect
        work = inspect.getsource(conduct.ConductorVoice._work)
        self.assertNotIn("_with_verbatim(", work,
                         "the seam re-applies the backstop")
        self.assertIn("last_utterance", work)


if __name__ == "__main__":
    unittest.main()
