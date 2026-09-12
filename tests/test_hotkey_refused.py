"""A refused event tap is explained, not swallowed.

hotkey.py needs Input Monitoring for its event tap. Without it macOS
refuses the tap, hotkey.py exits, and the conductor - which waits on the
hotkey - shuts down with it. Opened from Finder that was an app that
appeared and vanished, with the reason in a log file nobody reads, and
the next launch did the same without asking for the grant again (the
permission ask is once per grant). A friend's "the Fn key does not work".

So hotkey.py names the grant it was refused, the listener keeps it, and
conduct.py says so - in a dialog with the pane, when there is no terminal
- before it quits, every time it happens.

Run with:  python3 -m unittest tests.test_hotkey_refused -v
"""

from __future__ import annotations

import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import hotkey
from conductor import gui_permissions
from conductor.gui_permissions import PANES, explain_refusal

try:
    import voice_agent
except ImportError:            # the audio stack is not installed
    voice_agent = None

ROOT = Path(__file__).resolve().parent.parent
HEYGENT = {"__CFBundleIdentifier": "ai.heygent.conductor"}
TERMINAL = {"__CFBundleIdentifier": "com.apple.Terminal",
            "TERM_PROGRAM": "Apple_Terminal"}


class HotkeyNamesTheGrant(unittest.TestCase):
    """hotkey.py, run against a Quartz whose tap creation says no."""

    def refused_line(self) -> dict:
        quartz = types.SimpleNamespace(
            CGEventTapCreate=lambda *a: None,
            kCGSessionEventTap=1, kCGHeadInsertEventTap=0,
            kCGEventTapOptionListenOnly=1, kCGEventFlagsChanged=12,
            CGEventMaskBit=lambda bit: 1 << bit)
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"Quartz": quartz}), \
                mock.patch.object(sys, "stdout", out):
            code = hotkey.main()
        self.assertEqual(code, 1)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(lines), 1, "the refusal and nothing else")
        return lines[0]

    def test_a_refused_tap_is_an_error_naming_input_monitoring(self) -> None:
        event = self.refused_line()
        self.assertEqual(event["error"], "event tap refused")
        self.assertEqual(event["grant"], "input_monitoring")
        self.assertIn("Input Monitoring", event["hint"])

    def test_the_hint_does_not_presume_a_terminal(self) -> None:
        """The old hint said "your terminal app"; from Finder there is
        none, and the row to turn on is heygent's."""
        self.assertNotIn("terminal", self.refused_line()["hint"].lower())


@unittest.skipIf(voice_agent is None, "the audio stack is not installed")
class TheListenerKeepsIt(unittest.TestCase):
    class FakeAgent:
        def key_changed(self, down: bool, double: bool = False) -> None:
            pass

    def read(self, *lines):
        listener = voice_agent.HotkeyListener([], self.FakeAgent())
        listener.proc = type("P", (), {
            "stdout": iter([line.encode() for line in lines])})()
        with mock.patch.object(sys, "stderr", io.StringIO()):
            listener._read_events()
        return listener

    def test_a_refusal_is_remembered_by_its_grant(self) -> None:
        listener = self.read(
            '{"error": "event tap refused", "grant": "input_monitoring", '
            '"hint": "..."}\n')
        self.assertEqual(listener.refused_grant, "input_monitoring")

    def test_an_ordinary_run_refuses_nothing(self) -> None:
        listener = self.read('{"ready": true}\n', '{"fn": true}\n',
                             '{"fn": false}\n')
        self.assertIsNone(listener.refused_grant)

    def test_an_error_without_a_grant_is_not_a_refusal(self) -> None:
        listener = self.read('{"error": "something else"}\n')
        self.assertIsNone(listener.refused_grant)


class ExplainingIt(unittest.TestCase):
    def setUp(self) -> None:
        self.opened: list[str] = []
        self.said: list[str] = []
        self.registered: list[str] = []
        self.shown: list[tuple[str, tuple]] = []

    def explain(self, env, answer=None):
        def dialog(text, buttons):
            self.shown.append((text, buttons))
            return answer
        return explain_refusal(
            "input_monitoring", env=env,
            opener=lambda url: self.opened.append(url) or True,
            announce=self.said.append, register=self.registered.append,
            dialog=dialog if answer is not None else None)

    def test_from_finder_a_dialog_says_which_row_and_offers_the_pane(
            self) -> None:
        ask = self.explain(HEYGENT, gui_permissions.OPEN_SETTINGS)
        text, buttons = self.shown[0]
        self.assertEqual(buttons, (gui_permissions.QUIT,
                                   gui_permissions.OPEN_SETTINGS))
        self.assertIn("Fn", text)
        self.assertIn("Input Monitoring", text)
        self.assertIn("switch next to heygent", text)
        self.assertIn("click + and choose heygent", text)
        self.assertIn("open it again", text)
        self.assertNotIn("terminal", text.lower())
        self.assertEqual(self.registered, ["input_monitoring"],
                         "the row is put in the list before the pane opens")
        self.assertEqual(self.opened, [PANES["input_monitoring"]])
        self.assertTrue(ask.opened)
        self.assertEqual(self.said, [], "nothing printed to the log file")

    def test_quit_leaves_settings_closed(self) -> None:
        ask = self.explain(HEYGENT, gui_permissions.QUIT)
        self.assertEqual(self.opened, [])
        self.assertFalse(ask.opened)
        self.assertEqual(self.registered, ["input_monitoring"])

    def test_from_a_terminal_it_is_printed_and_the_pane_opens(self) -> None:
        ask = self.explain(TERMINAL)
        self.assertEqual(self.shown, [])
        self.assertEqual(len(self.said), 1)
        self.assertIn("Input Monitoring", self.said[0])
        self.assertIn("turn it on for Terminal", self.said[0])
        self.assertIn("restart your terminal", self.said[0])
        self.assertEqual(self.opened, [PANES["input_monitoring"]])
        self.assertTrue(ask.opened)

    def test_it_asks_every_time_unlike_the_startup_ask(self) -> None:
        """No memory: the tap has just been refused, so the grant is
        missing now, whatever was asked on an earlier launch."""
        self.explain(HEYGENT, gui_permissions.QUIT)
        self.explain(HEYGENT, gui_permissions.QUIT)
        self.assertEqual(len(self.shown), 2)


class WiredIntoConduct(unittest.TestCase):
    def test_conduct_explains_a_refusal_after_the_hotkey_ends(self) -> None:
        source = (ROOT / "conduct.py").read_text()
        waited = source.index("await hotkey.wait_with(session_task)")
        explained = source.index("explain_refusal, hotkey.refused_grant")
        self.assertLess(waited, explained)
        self.assertIn("dialog=None if dialogs.has_terminal() else "
                      "dialogs.alert)", source[explained:explained + 400])


if __name__ == "__main__":
    unittest.main()
