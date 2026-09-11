"""The caption helpers behind show_caption.

`caption_attrs`, `caption_measure` and `caption_tail` are called on every
`heard` message the voice agent sends. They were once deleted while their
call sites stayed, so the first spoken word raised a NameError inside the
overlay's timer and silently froze every later render. These pin them down.

Run with:  python3 -m unittest tests.test_overlay_caption -v
"""

from __future__ import annotations

import unittest

try:
    import overlay
    HAVE_APPKIT = True
except Exception:                       # headless CI without pyobjc
    HAVE_APPKIT = False


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class CaptionHelpersTest(unittest.TestCase):
    def test_a_short_caption_passes_through_untouched(self):
        self.assertEqual(overlay.caption_tail("hello there"), "hello there")

    def test_a_long_caption_keeps_its_tail_not_its_head(self):
        words = " ".join(f"word{i}" for i in range(80))
        tail = overlay.caption_tail(words)
        self.assertTrue(tail.startswith("… "))
        self.assertIn("word79", tail)
        self.assertNotIn("word0 ", tail)

    def test_the_tail_fits_the_caption_box(self):
        words = " ".join(f"word{i}" for i in range(80))
        limit = overlay.CAPTION_LINE * overlay.CAPTION_LINES + 4
        self.assertLessEqual(
            overlay.caption_measure(overlay.caption_tail(words)), limit)

    def test_measure_grows_with_wrapping(self):
        one = overlay.caption_measure("hi")
        two = overlay.caption_measure(" ".join(["hi"] * 40))
        self.assertGreater(two, one)
