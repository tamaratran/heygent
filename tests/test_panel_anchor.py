"""The panel's baseline holds while transient surfaces churn.

The spinner card ("Percolating…") and the live caption come and go every few
seconds of an ordinary session. The panel used to stack directly on top of
them, so every appearance shoved the worker cards ~100pt up and every
disappearance dropped them straight back down - the column visibly bounced.
Now the baseline rises at once when something below needs the room, but it
falls only after PANEL_SETTLE of quiet, gliding rather than jumping.

Run with:  python3 -m unittest tests.test_panel_anchor -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

try:
    import overlay
    HAVE_APPKIT = True
except Exception:                       # headless CI without pyobjc
    HAVE_APPKIT = False


def notice(i: int) -> dict:
    return {"id": f"n{i}", "task_id": f"t{i}", "title": f"proj · Task {i}",
            "body": "Working…", "status": "working"}


def spinner() -> dict:
    return {"title": "Percolating… (1s)", "title_style": "thinking",
            "status": "working", "fresh": True}


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class PanelAnchorTest(unittest.TestCase):
    def controller(self):
        c = overlay.Controller.alloc().init()
        c.show_notice(notice(1))
        c.show_notice(notice(2))
        c.render_panel()
        return c

    def test_the_spinner_lifts_the_panel_at_once(self):
        c = self.controller()
        resting = c.panel_base
        c.show_card(spinner())
        self.assertGreater(c.panel_base, resting)
        self.assertEqual(c.panel_base, c.stack_base())

    def test_the_spinner_leaving_does_not_drop_the_panel(self):
        c = self.controller()
        c.show_card(spinner())
        lifted = c.panel_base
        c.hide_card()
        self.assertEqual(c.panel_base, lifted)
        self.assertGreater(c.panel_settle_at, 0.0)

    def test_a_quick_return_cancels_the_settle(self):
        c = self.controller()
        c.show_card(spinner())
        lifted = c.panel_base
        c.hide_card()
        c.show_card(spinner())
        self.assertEqual(c.panel_base, lifted)
        self.assertEqual(c.panel_settle_at, 0.0)

    def test_after_the_hold_the_panel_glides_down(self):
        c = self.controller()
        resting = c.panel_base
        c.show_card(spinner())
        c.hide_card()
        now = c.panel_settle_at + 0.1
        heights = [c.panel_base]
        with patch.object(overlay.time, "monotonic", return_value=now):
            for _ in range(200):
                # A test's stdin is at EOF, so the reader thread queues a
                # quit that _drain would obey - and the run would silently
                # end here. The tick under test is the settle, not the drain.
                with c.lock:
                    c.pending.clear()
                c.tick_(None)
                heights.append(c.panel_base)
        self.assertEqual(c.panel_base, resting)
        drops = [a - b for a, b in zip(heights, heights[1:]) if a != b]
        self.assertGreater(len(drops), 5, "it glided, it did not jump")
        self.assertLess(max(drops), (heights[0] - resting) / 2)

    def test_an_empty_panel_forgets_its_baseline(self):
        c = self.controller()
        c.show_card(spinner())
        c.hide_card()
        c.dismiss_notice("n1")
        c.dismiss_notice("n2")
        c.render_panel()
        self.assertEqual(c.panel_base, 0.0)
        self.assertEqual(c.panel_settle_at, 0.0)
