"""A follow-up reaches the worker as the manager wrote it.

It did not. From 2026-08-31 send_to_task SENT the user's utterance in
place of any follow-up that was not the user's own words
(hold_to_users_words), because the manager had been seen wrapping
instructions in framing the user never said. On 2026-09-10 that rule fired
twelve times, and every time it cost the worker the instruction - the
Boss by then quoted the user and added only what the worker needed:

    Boss wrote   Confirmed: the recipient is Adrian. If exactly one Signal
                 contact is named Adrian, open that conversation and send
                 "hi". If there are several Adrians or none, stop and
                 report the exact names.
    worker got   Mm-hmm Ad yan

    Boss wrote   User: "Yes, just close PR two o one." Close #201 without
                 merging. Don't restart or merge #199 yet; the user hasn't
                 chosen that.
    worker got   Yes, just close PR two o one

and at 00:39:20Z a worker got two open utterances joined, the first of
them meant for a different worker. Whether the manager relayed the words
is now logged (manager.follow_up_paraphrased), never corrected.

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

from conductor.global_conductor import GlobalConductor, carries_users_words

SAID = "run the gh commands and tell me which PRs are in flight"

ADRIAN_SAID = "Mm-hmm Ad yan"
ADRIAN_WROTE = ('Confirmed: the recipient is Adrian. If exactly one Signal '
                'contact is named Adrian, open that conversation and send '
                '"hi". If there are several Adrians or none, stop and report '
                'the exact names.')
CLOSE_SAID = "Yes, just close PR two o one"
CLOSE_WROTE = ('User: "Yes, just close PR two o one." Close #201 without '
               "merging. Don't restart or merge #199 yet; the user hasn't "
               "chosen that.")


class MeasuringTheManagerAgainstTheUsersWords(unittest.TestCase):
    def test_the_users_own_sentence_carries_them(self):
        self.assertTrue(carries_users_words(SAID, [SAID]))

    def test_a_quoted_part_of_it_carries_them(self):
        """One utterance can be about two tasks; the manager routes a part
        by quoting it."""
        self.assertTrue(carries_users_words("tell me which PRs are in flight",
                                            [SAID]))

    def test_a_quote_with_context_carries_them(self):
        """How the Boss writes follow-ups now: the words, then the line of
        context the worker needs."""
        self.assertTrue(carries_users_words(CLOSE_WROTE, [CLOSE_SAID]))

    def test_punctuation_and_case_do_not_count_as_rewriting(self):
        self.assertTrue(carries_users_words(
            "Run the gh commands and tell me which PRs are in flight.", [SAID]))

    def test_a_paraphrase_does_not(self):
        self.assertFalse(carries_users_words(ADRIAN_WROTE, [ADRIAN_SAID]))

    def test_nothing_spoken_has_no_answer(self):
        """A typed turn, or one the manager raised itself, has no
        utterance to measure anything against."""
        self.assertIsNone(carries_users_words("carry on with the plan", []))
        self.assertIsNone(carries_users_words("carry on", ["  "]))


class ItHappensOnTheRealPath(unittest.TestCase):
    def conductor(self):
        gc = GlobalConductor.__new__(GlobalConductor)
        gc._utterances = []
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

    def events(self, gc):
        return [c.args[0] for c in gc._emit.call_args_list]

    def test_the_instruction_is_what_the_worker_gets(self):
        """The one that sent "Mm-hmm Ad yan" to a worker about to message
        someone on Signal."""
        gc, inner = self.conductor()
        gc._utterances = [ADRIAN_SAID]
        asyncio.run(gc.send_to_task("task_a", ADRIAN_WROTE))
        inner.send_to_task.assert_awaited_once_with("task_a", ADRIAN_WROTE)

    def test_the_context_after_a_yes_is_kept(self):
        gc, inner = self.conductor()
        gc._utterances = [CLOSE_SAID]
        asyncio.run(gc.send_to_task("task_a", CLOSE_WROTE))
        inner.send_to_task.assert_awaited_once_with("task_a", CLOSE_WROTE)
        self.assertNotIn("manager.follow_up_paraphrased", self.events(gc))

    def test_a_paraphrase_is_visible_in_the_log_and_still_sent(self):
        """How often the manager authors is still the number worth
        having; it is no longer a reason to throw its message away."""
        gc, inner = self.conductor()
        gc._utterances = [SAID]
        asyncio.run(gc.send_to_task("task_a", "The user wants PR status."))
        inner.send_to_task.assert_awaited_once_with(
            "task_a", "The user wants PR status.")
        self.assertIn("manager.follow_up_paraphrased", self.events(gc))
        self.assertNotIn("manager.follow_up_rewritten", self.events(gc))

    def test_a_typed_turn_logs_nothing(self):
        gc, inner = self.conductor()
        asyncio.run(gc.send_to_task("task_a", "carry on with the plan"))
        inner.send_to_task.assert_awaited_once_with("task_a",
                                                    "carry on with the plan")
        self.assertEqual(self.events(gc), [])

    def test_the_utterance_lasts_exactly_one_turn(self):
        """Left behind, it would be measured against the NEXT turn's
        follow-up."""
        gc, _ = self.conductor()

        async def handle(text, conductor):
            self.assertEqual(conductor._utterances, [SAID])
            return mock.Mock(tool_calls=[], reply="ok")
        gc.manager.handle = handle
        with mock.patch("conductor.global_conductor.new_trace"):
            asyncio.run(gc.handle_user_message("x", utterance=SAID))
        self.assertEqual(gc._utterances, [])
        self.assertEqual(gc._utterance, "")

    def test_the_utterance_is_cleared_even_when_the_turn_fails(self):
        gc, _ = self.conductor()

        async def handle(text, conductor):
            raise RuntimeError("boom")
        gc.manager.handle = handle
        with mock.patch("conductor.global_conductor.new_trace"):
            with self.assertRaises(RuntimeError):
                asyncio.run(gc.handle_user_message("x", utterance=SAID))
        self.assertEqual(gc._utterances, [])

    def test_overlapping_turns_never_put_one_turns_words_in_another(self):
        """Turns overlap: a visible Boss takes the next thing said mid-turn
        and may act on both at once. The joined utterance of two open
        turns once reached a worker the first turn had nothing to do with
        (00:39:20Z). Both follow-ups go as written."""
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
            [("task_a", "Here is what to do."), ("task_a", "And then this.")])
        self.assertEqual(gc._utterances, [])


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
