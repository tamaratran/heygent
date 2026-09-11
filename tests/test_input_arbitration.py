"""Two writers, one keyboard: the person in the workspace and the Manager.

Sections 18 and 42. cmux makes a worker's terminal something the user can
type into, which is the point of it - and also the hazard. send-keys does
not replace the input box, it APPENDS and then presses Enter. So a
follow-up arriving while somebody is mid-sentence submits their unfinished
words welded to ours, as one prompt neither of us wrote, and the half
they were still editing is gone.

The person wins: nothing of theirs is ever submitted. It used to mean the
follow-up waited for the box to clear - 45 seconds, then failed - and on
the Boss window two stray characters swallowed every spoken message for a
minute. Now their draft is set aside (Ctrl-U), ours goes in, and theirs is
typed back. Only text queued under RUNNING work still waits, because Enter
there would submit both.

Run with:  python3 -m unittest tests.test_input_arbitration -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from conductor.tmux_runtime import PROBE, TmuxClaudeRuntime, _typed_but_unsent

# Measured off a live worker: an empty box is the prompt character and
# U+00A0, nothing else.
EMPTY = "❯\xa0"
READY = f"""
  Bottom line: everything is pushed.

{EMPTY}
  ⏵⏵ auto mode on (shift+tab to cycle)
"""
TYPING = READY.replace(EMPTY, "❯\xa0also check the billing tests")


class ReadingTheInputBox(unittest.TestCase):
    def test_an_empty_box_is_not_someone_typing(self):
        self.assertEqual(_typed_but_unsent(READY), "")

    def test_half_a_sentence_is(self):
        self.assertEqual(_typed_but_unsent(TYPING),
                         "also check the billing tests")

    def test_the_providers_own_hint_text_is_not_input(self):
        """The placeholder sits in the box exactly where typing does."""
        hint = READY.replace(EMPTY, '❯\xa0Try "how do I add a test?"')
        self.assertEqual(_typed_but_unsent(hint), "")

    def test_a_queued_message_is_not_mistaken_for_someone_typing(self):
        """Measured: a follow-up delivered while the worker is busy is
        QUEUED, and Claude Code leaves "Press up to edit queued messages"
        sitting in the input box. Reading that as the user being
        mid-sentence would hold back every follow-up after the first."""
        queued = READY.replace(EMPTY, "❯\xa0Press up to edit queued messages")
        self.assertEqual(_typed_but_unsent(queued), "")

    def test_transcript_text_is_not_mistaken_for_the_box(self):
        """Only the last prompt line counts; the conversation above it is
        full of lines that start with the same characters."""
        pane = READY.replace("Bottom line", "> quoted from a diff\n  > more")
        self.assertEqual(_typed_but_unsent(pane), "")


class WaitingForTheUserToFinish(unittest.TestCase):
    def runtime(self, panes, after_probe=None):
        """panes: what each capture returns, in order (the last repeats).
        after_probe: what the box shows once the probe character is in;
        None means the probe is appended, the way a real box behaves
        when the text was typed."""
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        rt._handle_startup_prompts = lambda sess: None
        self.reads = 0
        self.keys = []
        self.probing = False

        def tmux(verb, *args):
            if verb == "send-keys":
                self.keys.append(args[-1])
                if args[-1] == PROBE:
                    self.probing = True
                elif args[-1] == "BSpace":
                    self.probing = False
                return mock.Mock(returncode=0)
            self.reads += 1
            pane = panes[min(self.reads - 1, len(panes) - 1)]
            if self.probing:
                pane = after_probe if after_probe is not None \
                    else pane.replace("\n  ⏵⏵", PROBE + "\n  ⏵⏵", 1)
            return mock.Mock(stdout=pane)
        rt._tmux = tmux
        return rt

    def test_a_draft_is_handed_back_rather_than_waited_on(self):
        """The person still wins - nothing of theirs is ever submitted -
        but the message no longer waits for them. Measured on the Boss
        window: two stray characters and every spoken message for the
        next minute died with "someone is typing there". The wait returns
        the draft; send sets it aside with Ctrl-U, delivers, and types it
        back (tests/test_tmux_runtime: a draft is set aside and put back)."""
        rt = self.runtime([TYPING])
        sess = mock.Mock(name_="s")
        sess.name = "cond_task_a"
        draft = asyncio.run(rt._wait_for_input(sess, timeout=5.0))
        self.assertEqual(draft, "also check the billing tests")
        self.assertEqual(self.reads, 2, "waited on the user")  # look, probe look

    def test_a_queued_message_under_running_work_still_waits(self):
        """Text in the box while the worker is generating is a message
        queued behind the current turn, not an idle draft. Enter would
        submit both into the queue, so that case does wait."""
        rt = self.runtime([TYPING.replace(
            "Bottom line", "✻ Thinking… (esc to interrupt)\n  Bottom line")])
        sess = mock.Mock()
        sess.name = "cond_task_a"
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(rt._wait_for_input(sess, timeout=1.0))
        self.assertIn("never returned to its prompt", str(caught.exception))

    def test_an_idle_worker_is_not_kept_waiting(self):
        rt = self.runtime([READY])
        sess = mock.Mock()
        sess.name = "cond_task_a"
        asyncio.run(rt._wait_for_input(sess, timeout=5.0))
        self.assertEqual(self.reads, 1)

    def test_a_probe_tells_a_person_from_a_suggestion(self):
        """Typing appends to a person's words; a probe on typed text
        leaves both there, so this IS a person - and their draft is handed
        back to be set aside and restored, with the probe character taken
        back out first. Nothing waits on them any more."""
        rt = self.runtime([TYPING, TYPING, READY])
        sess = mock.Mock()
        sess.name = "cond_task_a"
        draft = asyncio.run(rt._wait_for_input(sess, timeout=5.0))
        self.assertEqual(self.keys, [PROBE, "BSpace"])
        self.assertEqual(draft, "also check the billing tests")
        self.assertEqual(self.reads, 2)          # one look, one probe look


# Measured on the live Boss after a recap: Claude Code's suggested next
# prompt sits in the box and is byte-for-byte what typing looks like.
GHOST = READY.replace(EMPTY, "❯\xa0Close the last four and read the PR answer")
PROBED_GHOST = READY.replace(EMPTY, "❯\xa0" + PROBE)


class ASuggestionIsNotAPerson(unittest.TestCase):
    def runtime(self, *a, **k):
        return WaitingForTheUserToFinish.runtime(self, *a, **k)

    def test_the_recap_suggestion_does_not_hold_the_message(self):
        """Typing replaces a suggestion, so the probe alone shows: the
        message goes in now, over the suggestion, rather than waiting 45 s
        and being dropped as 'someone is typing there'."""
        rt = self.runtime([GHOST], after_probe=PROBED_GHOST)
        sess = mock.Mock()
        sess.name = "cond_boss"
        asyncio.run(rt._wait_for_input(sess, timeout=2.0))
        self.assertEqual(self.keys, [PROBE, "BSpace"])
        self.assertEqual(self.reads, 2)          # one look, one probe look

    def test_the_box_is_probed_once_not_every_poll(self):
        rt = self.runtime([TYPING, TYPING, TYPING, READY])
        sess = mock.Mock()
        sess.name = "cond_boss"
        asyncio.run(rt._wait_for_input(sess, timeout=5.0))
        self.assertEqual(self.keys.count(PROBE), 1)

    def test_a_person_typing_after_a_recap_still_wins(self):
        """The recap is on screen either way; only the probe knows. A
        person's words are never submitted - they come back as the draft
        to set aside and restore, rather than being typed over."""
        typed_after_recap = GHOST.replace("Close the last four and read the PR answer",
                                          "actually, first run the tests")
        rt = self.runtime([typed_after_recap])
        sess = mock.Mock()
        sess.name = "cond_boss"
        draft = asyncio.run(rt._wait_for_input(sess, timeout=1.0))
        self.assertEqual(draft, "actually, first run the tests")
        self.assertEqual(self.keys, [PROBE, "BSpace"])


if __name__ == "__main__":
    unittest.main()
