"""An overlay must never outlive its agent, or become undismissable.

Reimplements the fixes from PR #2 against the current overlay. The failures
it described are all still reachable: every stdout write was unguarded, so
the first dismissal after the agent died killed the overlay with a
BrokenPipeError before the card came off screen, leaving an orphan on top of
everything that could only be cleared with pkill.

Run with:  python3 -m unittest tests.test_overlay_shutdown -v
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path

try:
    import overlay
    HAVE_APPKIT = True
except Exception:
    HAVE_APPKIT = False


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class ReportingTest(unittest.TestCase):
    def test_a_dead_pipe_does_not_raise(self) -> None:
        import sys

        class Dead(io.TextIOBase):
            def write(self, _s):
                raise BrokenPipeError(32, "Broken pipe")

        original, sys.stdout = sys.stdout, Dead()
        try:
            self.assertFalse(overlay.report(event="dismiss"))
        finally:
            sys.stdout = original

    def test_a_live_pipe_reports_true(self) -> None:
        import sys
        original, sys.stdout = sys.stdout, io.StringIO()
        try:
            self.assertTrue(overlay.report(event="dismiss", id="x"))
            written = sys.stdout.getvalue()
        finally:
            sys.stdout = original
        self.assertIn('"event": "dismiss"', written)

    def test_nothing_writes_to_stdout_directly(self) -> None:
        """One guarded path out, so a new call site cannot reintroduce it."""
        source = Path(overlay.__file__).read_text()
        self.assertEqual(source.count("sys.stdout.write"), 1,
                         "a write bypasses report() and can kill the overlay")


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class DismissOrderTest(unittest.TestCase):
    def test_the_card_hides_before_the_agent_is_told(self) -> None:
        source = Path(overlay.__file__).read_text()
        body = source[source.index("def dismiss_card"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertLess(body.index("self.hide_card()"),
                        body.index("report("),
                        "reporting first means a dead pipe leaves the card up")


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class ShutdownTest(unittest.TestCase):
    def test_there_is_always_a_way_out(self) -> None:
        source = Path(overlay.__file__).read_text()
        self.assertIn("Quit Voice Agent", source)
        # Both menu branches, so it is there even with no activity at all.
        self.assertEqual(source.count("self._add_quit_item(menu)"), 2)

    def test_it_leaves_when_its_parent_does(self) -> None:
        source = Path(overlay.__file__).read_text()
        body = source[source.index("def _watch_parent"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn("os.getppid()", body)
        self.assertIn('{"state": "quit"}', body)

    def test_stdin_eof_still_quits(self) -> None:
        source = Path(overlay.__file__).read_text()
        body = source[source.index("def _read_stdin"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn('{"state": "quit"}', body)
