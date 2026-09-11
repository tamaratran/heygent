"""Markdown never reaches a card, a notification or the voice.

Run with:  python3 -m unittest tests.test_plain_text -v
"""

from __future__ import annotations

import unittest

from conductor.notifications import concise
from conductor.plain_text import plain_text
from conductor.session_card import status_text


class PlainTextTest(unittest.TestCase):
    def test_the_measured_bodies(self):
        """Straight out of notifications.json."""
        self.assertEqual(
            plain_text("**No — nothing mentioning FFX has landed.** I fetched "
                       "origin"),
            "No — nothing mentioning FFX has landed. I fetched origin")
        self.assertEqual(
            plain_text("Here's the summary. ## PRs #36, #37, #38\n- one\n- two"),
            "Here's the summary. ## PRs #36, #37, #38\none\ntwo")
        self.assertEqual(
            plain_text("runs (the ones using the repo's `.venv`, before I "
                       "built the scratch venv)"),
            "runs (the ones using the repo's .venv, before I built the "
            "scratch venv)")

    def test_headings_bullets_links_and_fences(self):
        text = ("# Title\n\n### Sub\n\n```bash\nls\n```\n\n"
                "* a bullet\n1. a number\n[docs](https://x.y/z) and __under__")
        self.assertEqual(plain_text(text),
                         "Title\n\nSub\n\nls\n\na bullet\n1. a number\n"
                         "docs and under")

    def test_words_that_only_look_like_markdown_survive(self):
        for text in ("snake_case_name stays", "2 * 3 = 6", "a_b and c_d",
                     "PR #36 and #37", "wait - then go"):
            self.assertEqual(plain_text(text), text)

    def test_nested_and_empty(self):
        self.assertEqual(plain_text("***both*** and **`code`**"),
                         "both and code")
        self.assertEqual(plain_text(""), "")

    def test_a_notification_one_liner_is_plain(self):
        self.assertEqual(concise("**Done.** Tests `pass`."), "Done. Tests pass.")

    def test_a_card_status_line_is_plain(self):
        state = {"task_id": "task_a", "status": "completed",
                 "result": {"summary": "**All green** — 12 tests."}}
        self.assertEqual(status_text(state), "Completed — All green — 12 tests")

    def test_an_answered_card_is_plain_too(self):
        state = {"task_id": "task_a", "status": "idle",
                 "result": {"summary": "`tests` **pass**."}}
        self.assertEqual(status_text(state), "Answered — tests pass")


if __name__ == "__main__":
    unittest.main()
