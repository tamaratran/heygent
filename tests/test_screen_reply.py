"""The Boss's reply read off its screen, line by line.

The frames under tests/fakes/boss_screens are real captures (tmux
capture-pane, every 150 ms) of Claude Code 2.1.268 in the Boss's layout -
fullscreen, focus view, an 80x24 pane - taken 2026-09-10:

    lighthouse/  a reply of prose, a bulleted list, prose; no tools
    sea/         a tool call, then 30 numbered lines that scroll the
                 screen faster than it is read
    boss_after_tools.txt   the live Boss after two turns

Run with:  python3 -m unittest tests.test_screen_reply -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

from conductor.screen_reply import ReplyDraft, merge, reply_lines

SCREENS = Path(__file__).parent / "fakes" / "boss_screens"


def frames(name: str) -> list[str]:
    return [p.read_text() for p in sorted((SCREENS / name).glob("*.txt"))]


LIGHTHOUSE = "Write about 12 lines of plain prose about lighthouses"
SEA = "First say one sentence saying you will look"


def play(name: str, said: str) -> list[str]:
    draft = ReplyDraft(said)
    return [text for text in map(draft.feed, frames(name))
            if text is not None]


class TheScreenIsRead(unittest.TestCase):
    def test_a_reply_grows_line_by_line(self):
        drafts = play("lighthouse", LIGHTHOUSE)
        self.assertGreater(len(drafts), 8, "not line by line")
        for before, after in zip(drafts, drafts[1:]):
            self.assertTrue(after.startswith(before.rstrip("-").rstrip()),
                            f"a draft went backwards:\n{before}\n---\n{after}")
        self.assertTrue(drafts[0].startswith("A lighthouse is a promise"))
        self.assertTrue(drafts[-1].endswith("will look up and see."))
        self.assertIn("- The keeper: Keepers trimmed wicks", drafts[-1])

    def test_a_blank_redraw_takes_nothing_away(self):
        """Measured: one frame mid-reply came back with the prose gone."""
        self.assertNotIn("", play("lighthouse", LIGHTHOUSE))

    def test_status_notices_and_tools_are_not_prose(self):
        text = "\n".join(play("sea", SEA))
        for chrome in ("Clauding", "Searching for 1 pattern", "Finding",
                       "tmux", "Fast mode", "bypass permissions", "⎿"):
            self.assertNotIn(chrome, text)
        self.assertEqual(reply_lines((SCREENS / "boss_after_tools.txt")
                                     .read_text())[0],
                         "No, 198 isn't merged. Its conflicts are fixed and "
                         "it's ready, but it's waiting")

    def test_the_last_prompt_bounds_the_reply(self):
        lines = reply_lines((SCREENS / "boss_after_tools.txt").read_text())
        self.assertNotIn("On it.", "\n".join(lines))
        self.assertEqual(lines[-1], "restart together.")

    def test_a_tool_group_line_is_not_prose(self):
        screen = ("❯ hi\n\n  Called boss 2 times\n\n⏺ Hello.\n\n"
                  + "─" * 80 + "\n❯ \n" + "─" * 80 + "\n")
        self.assertEqual(reply_lines(screen), ["Hello."])

    def test_a_layout_it_cannot_read_gives_nothing(self):
        self.assertIsNone(reply_lines("$ claude\nHello there\n"))
        draft = ReplyDraft()
        self.assertIsNone(draft.feed("$ claude\nHello there\n"))


class TheDraftIsAnchored(unittest.TestCase):
    def test_the_previous_reply_is_not_this_turns(self):
        """Until this turn's words are on screen, the reply below the last
        prompt is the one before."""
        old = (SCREENS / "boss_after_tools.txt").read_text()
        draft = ReplyDraft("what is open right now")
        self.assertIsNone(draft.feed(old))
        self.assertEqual(draft.text(), "")

    def test_words_still_in_the_input_box_do_not_anchor(self):
        first = frames("lighthouse")[0]          # typed, not yet sent
        draft = ReplyDraft(LIGHTHOUSE)
        self.assertIsNone(draft.feed(first))


class TheTranscriptCorrectsTheScreen(unittest.TestCase):
    def test_lines_that_scrolled_past_arrive_with_their_paragraph(self):
        """Thirty lines landed between two reads of a 24-row screen;
        the first fifteen were never on it."""
        draft = ReplyDraft(SEA)
        for screen in frames("sea"):
            draft.feed(screen)
        self.assertTrue(draft.text().startswith("16. Sunlight"))
        whole = "\n".join(f"{n}. line {n}" for n in range(1, 31))
        committed = draft.commit(whole + "\n\nLike the tide, these lines "
                                 "end here, and there's always more to find.")
        self.assertTrue(committed.startswith("1. line 1\n"))
        self.assertNotIn("Sunlight", committed, "the screen's copy stayed")

    def test_a_committed_paragraph_replaces_its_lines(self):
        draft = ReplyDraft()
        box = "\n" + "─" * 40 + "\n❯ \n" + "─" * 40 + "\n"
        draft.feed("❯ go\n\n⏺ Starting a **worker**\n  on it." + box)
        self.assertEqual(draft.text(), "Starting a **worker**\non it.")
        self.assertEqual(draft.commit("Starting a **worker** on it."),
                         "Starting a **worker** on it.")
        draft.feed("❯ go\n\n⏺ Starting a worker\n  on it.\n\n  Called boss"
                   "\n\n⏺ It is" + box)
        self.assertEqual(draft.text(), "Starting a **worker** on it.\n\nIt is")

    def test_a_screen_behind_the_transcript_shows_nothing_twice(self):
        draft = ReplyDraft()
        box = "\n" + "─" * 40 + "\n❯ \n" + "─" * 40 + "\n"
        draft.commit("One paragraph that is long enough to match here.")
        draft.commit("Two paragraphs, also long enough to be matched.")
        self.assertIsNone(draft.feed(
            "❯ go\n\n⏺ One paragraph that is long enough to match here."
            + box))


class LinesAreMerged(unittest.TestCase):
    def test_a_growing_last_line(self):
        self.assertEqual(merge(["a b", "c"], ["a b", "c d", "e"]),
                         ["a b", "c d", "e"])

    def test_lines_scrolled_off_the_top_stay(self):
        self.assertEqual(merge(["1", "2", "3"], ["2", "3", "4"]),
                         ["1", "2", "3", "4"])

    def test_nothing_new_keeps_the_old(self):
        self.assertEqual(merge(["1", "2"], []), ["1", "2"])
        self.assertEqual(merge(["1", "2"], ["2"]), ["1", "2"])


if __name__ == "__main__":
    unittest.main()
