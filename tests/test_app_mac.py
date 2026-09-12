"""The Boss window's own drag handling (conductor/app_mac.py).

The page is never told a dropped file's path - WebKit keeps it - so the
window takes the drag itself and reads the paths off the pasteboard.
These tests do that for real: a genuine NSPasteboard written the way
Finder writes one, handed to a genuine web view, with no window on the
screen and nothing dragged by hand.

Run with:  python3 -m unittest tests.test_app_mac -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

try:
    import objc
    from AppKit import (NSDragOperationCopy, NSMakeRect, NSObject,
                        NSPasteboard, NSPasteboardTypeFileURL,
                        NSPasteboardTypePNG, NSPasteboardTypeString)
    from Foundation import NSURL, NSData
    from WebKit import WKWebViewConfiguration
    from conductor import app_mac
except ImportError:                             # no AppKit/WebKit here
    app_mac = None
else:
    class FakeDrag(NSObject):
        """An NSDraggingInfo as far as the view is concerned."""

        def initWithBoard_(self, board):
            self = objc.super(FakeDrag, self).init()
            self._board = board
            return self

        def draggingPasteboard(self):
            return self._board

    def fresh_board():
        board = NSPasteboard.pasteboardWithUniqueName()
        board.clearContents()
        return board

    def board_of(*paths):
        board = fresh_board()
        board.writeObjects_([NSURL.fileURLWithPath_(str(p)) for p in paths])
        return board


@unittest.skipIf(app_mac is None, "AppKit and WebKit are not installed")
class WhatWasDropped(unittest.TestCase):
    def test_dropped_files_arrive_as_their_real_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            one = Path(folder) / "a shot.png"
            two = Path(folder) / "notes.md"
            one.write_bytes(b"png")
            two.write_text("notes")
            board = board_of(one, two)
            self.assertTrue(app_mac._dropped_files(board))
            self.assertEqual(app_mac._paths_on(board), [str(one), str(two)])

    def test_dragged_text_is_left_to_the_page(self):
        board = fresh_board()
        board.setString_forType_("just words", NSPasteboardTypeString)
        self.assertFalse(app_mac._dropped_files(board))

    def test_a_picture_with_no_file_behind_it_is_written_to_one(self):
        """A picture dragged out of another app - a browser, Preview -
        has no file to name, so one is written for it."""
        board = fresh_board()
        png = b"\x89PNG\r\n\x1a\n" + b"pretend"
        board.setData_forType_(
            NSData.dataWithBytes_length_(png, len(png)), NSPasteboardTypePNG)
        self.assertTrue(app_mac._dropped_files(board))
        with tempfile.TemporaryDirectory() as folder:
            paths = app_mac._paths_on(board, Path(folder))
            self.assertEqual(len(paths), 1)
            kept = Path(paths[0])
            self.assertEqual(kept.parent, Path(folder))
            self.assertEqual(kept.read_bytes(), png)
            self.assertEqual(kept.suffix, ".png")

    def test_an_empty_pasteboard_is_nobody_s_drop(self):
        self.assertFalse(app_mac._dropped_files(fresh_board()))
        self.assertEqual(app_mac._paths_on(fresh_board()), [])


@unittest.skipIf(app_mac is None, "AppKit and WebKit are not installed")
class TheWindowsWebView(unittest.TestCase):
    """The real view, off screen, walked through a real drag."""

    def view(self):
        return app_mac.DropWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, 400, 300), WKWebViewConfiguration.new())

    def dragging(self, board):
        return FakeDrag.alloc().initWithBoard_(board)

    def test_a_dropped_file_is_taken_by_the_window(self):
        """Every step AppKit asks for: the view says it will copy, then
        takes the drop rather than leaving it to WebKit - which would
        navigate the window to the file and lose the conversation."""
        with tempfile.TemporaryDirectory() as folder:
            shot = Path(folder) / "a shot.png"
            shot.write_bytes(b"png")
            view, info = self.view(), self.dragging(board_of(shot))
            self.assertEqual(view.draggingEntered_(info), NSDragOperationCopy)
            self.assertEqual(view.draggingUpdated_(info), NSDragOperationCopy)
            self.assertTrue(view.prepareForDragOperation_(info))
            self.assertTrue(view.performDragOperation_(info))

    def test_dragged_text_is_handed_straight_back_to_WebKit(self):
        """A drag with no file in it is WebKit's - and WebKit really is
        asked: it goes on to read the drag itself, which this stand-in
        for a real NSDraggingInfo cannot answer."""
        board = fresh_board()
        board.setString_forType_("just words", NSPasteboardTypeString)
        view, info = self.view(), self.dragging(board)
        with self.assertRaises(ValueError) as caught:
            view.draggingEntered_(info)
        self.assertIn("draggingLocation", str(caught.exception))

    def test_a_drag_WebKit_has_no_answer_for_is_simply_a_no(self):
        self.assertEqual(
            app_mac._webkits(self.view(), "noSuchDragMethod_", None, "no"),
            "no")

    def test_the_window_s_drag_strip_is_not_a_dead_band(self):
        """The strip that moves the window lies over the web view; a
        file dropped on it is the view's all the same."""
        with tempfile.TemporaryDirectory() as folder:
            shot = Path(folder) / "shot.png"
            shot.write_bytes(b"png")
            board = board_of(shot)
            view = self.view()
            strip = app_mac.DragStrip.alloc().initWithFrame_over_(
                NSMakeRect(0, 0, 400, 28), view)
            self.assertIn(NSPasteboardTypeFileURL,
                          list(strip.registeredDraggedTypes()))
            info = self.dragging(board)
            self.assertEqual(strip.draggingEntered_(info),
                             NSDragOperationCopy)
            self.assertTrue(strip.performDragOperation_(info))

    def test_the_paths_reach_the_box_through_the_page(self):
        script = app_mac._insert_script(["/tmp/a b.png", "/tmp/b.md"])
        self.assertTrue(script.startswith("window.insertPaths &&"))
        self.assertEqual(json.loads(script.split("insertPaths(", 1)[1][:-1]),
                         ["/tmp/a b.png", "/tmp/b.md"])


@unittest.skipIf(app_mac is None, "AppKit and WebKit are not installed")
class NamedInTheDock(unittest.TestCase):
    """The Dock's tooltip under our icon said python3.13: a process is
    named after the bundle its executable is in, and a uv script's
    executable is the interpreter. So the window starts over through a
    symlink to that same interpreter inside a heygent-window.app."""

    def test_the_window_relaunches_as_heygent(self):
        import plistlib
        import sys
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            done = app_mac.relaunch_named(
                home, env={"PATH": "/usr/bin", "PYTHONPATH": "/x"},
                argv=["conductor/app_mac.py", "--stdio"],
                execve=lambda *a: calls.append(a))
            self.assertTrue(done)
            path, argv, env = calls[0]
            link = home / "heygent-window.app" / "Contents" / "MacOS" / "heygent"
            self.assertEqual(path, str(link))
            self.assertEqual(argv, [str(link), "conductor/app_mac.py",
                                    "--stdio"])
            self.assertEqual(Path(link).resolve(),
                             Path(sys.executable).resolve(),
                             "the very same interpreter")
            info = plistlib.loads(
                (home / "heygent-window.app" / "Contents" / "Info.plist")
                .read_bytes())
            self.assertEqual(info["CFBundleName"], "heygent")
            self.assertEqual(info["CFBundleExecutable"], "heygent")
            self.assertEqual(env["HEYGENT_WINDOW_RELAUNCHED"], "1")
            self.assertTrue(env["PYTHONPATH"].endswith(":/x"),
                            "the venv's site-packages come first")
            self.assertIn("site-packages", env["PYTHONPATH"].split(":")[0])
            # The relaunched process must not relaunch again.
            self.assertFalse(app_mac.relaunch_named(
                home, env=env, argv=[],
                execve=lambda *a: self.fail("relaunched twice")))
            self.assertEqual(len(calls), 1)

    def test_a_home_that_cannot_be_written_stays_python(self):
        with tempfile.TemporaryDirectory() as folder:
            blocker = Path(folder) / "file"
            blocker.write_text("")
            self.assertFalse(app_mac.relaunch_named(
                blocker / "home", env={},
                execve=lambda *a: self.fail("exec with no bundle")))


if __name__ == "__main__":
    unittest.main()
