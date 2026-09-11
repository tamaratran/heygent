"""The notifications are out of the way until they are asked for.

They float above every other app on the machine, in the middle of whatever
the user is actually working in, and they used to do that from the moment
the agent started - the reply card and the stack of task notifications,
over the top of their editor, whether or not they were talking to it. So
they start hidden, and a double tap on Fn toggles them (the gesture itself
lives in hotkey.py; see tests.test_hotkey).

The capsule and the caption are not notifications. They say the microphone
is open and what it heard, so they show whenever the user holds Fn, hidden
notifications or not.

Hiding is about the screen and nothing else. Every window keeps the state
it had, so what comes back is what was there rather than an empty screen
and a notification nobody ever saw. That is the half these tests are really
about: the flags that say what *should* be up must survive.

Run with:  python3 -m unittest tests.test_overlay_visibility -v
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

try:
    import overlay
    HAVE_APPKIT = True
except Exception:                       # headless CI without pyobjc
    HAVE_APPKIT = False

try:                       # the audio stack is not installed everywhere
    import voice_agent
except Exception:          # pragma: no cover - exercised by skipping
    voice_agent = None

# Source-reading tests need no AppKit: the file, not the module.
OVERLAY_SOURCE = Path(__file__).resolve().parent.parent / "overlay.py"

# Every window the overlay owns, with the flag that says whether it belongs
# on screen. The status-bar bell is deliberately not among them.
WINDOWS = {"window": "visible",
           "caption_window": "caption_visible",
           "card_window": "card_visible",
           "panel_window": "panel_visible",
           "badge_window": "badge_visible",
           "chevron_window": "chevron_visible"}
# The ones a double tap hides. Spelled out here rather than read from
# overlay.NOTIFICATION_WINDOWS, so a change there has to be made on purpose.
NOTIFICATIONS = {"card_window", "panel_window", "badge_window",
                 "chevron_window"}


class FakeWindow:
    def __init__(self) -> None:
        self.on_screen = False

    def orderFrontRegardless(self) -> None:
        self.on_screen = True

    def orderOut_(self, _sender) -> None:
        self.on_screen = False


class Stub:
    """Only what the visibility methods touch, so no AppKit is started.

    Instantiating the real Controller would open six windows and a status
    item on the machine running the tests.
    """

    def __init__(self, hidden: bool = True, up: tuple = ()) -> None:
        self.hidden = hidden
        self.width = 0.0
        self.target_width = 320.0
        self.resized = 0
        for window, flag in WINDOWS.items():
            setattr(self, window, FakeWindow())
            setattr(self, flag, flag in up or window in up)
            showing = window not in NOTIFICATIONS or not hidden
            if getattr(self, flag) and showing:
                getattr(self, window).on_screen = True

    def _resize(self) -> None:
        self.resized += 1

    def on_screen(self) -> set:
        return {w for w in WINDOWS if getattr(self, w).on_screen}

    def flags(self) -> set:
        return {f for f in WINDOWS.values() if getattr(self, f)}


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class VisibilityTest(unittest.TestCase):

    def setUp(self) -> None:
        self.reported = []
        self._real_report = overlay.report
        overlay.report = lambda **event: self.reported.append(event) or True
        self.addCleanup(setattr, overlay, "report", self._real_report)

    def hide(self, stub, hidden: bool) -> None:
        overlay.Controller.set_hidden(stub, hidden)

    def test_the_notification_windows_are_the_ones_named_here(self) -> None:
        self.assertEqual(set(overlay.NOTIFICATION_WINDOWS), NOTIFICATIONS)
        for name, flag in overlay.NOTIFICATION_WINDOWS.items():
            self.assertEqual(WINDOWS[name], flag)

    # -- the gate every orderFront goes through ---------------------------
    def test_hidden_notifications_show_nothing(self) -> None:
        stub = Stub(hidden=True)
        for name in NOTIFICATIONS:
            overlay.Controller._show_window(stub, getattr(stub, name))
        self.assertEqual(stub.on_screen(), set())

    def test_the_capsule_and_caption_show_while_notifications_hide(
            self) -> None:
        """The user asked for this: hide the notifications, never the
        capsule. Holding Fn with notifications away still shows the words
        being heard."""
        stub = Stub(hidden=True)
        overlay.Controller._show_window(stub, stub.window)
        overlay.Controller._show_window(stub, stub.caption_window)
        self.assertEqual(stub.on_screen(), {"window", "caption_window"})

    def test_shown_notifications_show_what_they_are_given(self) -> None:
        stub = Stub(hidden=False)
        overlay.Controller._show_window(stub, stub.card_window)
        self.assertTrue(stub.card_window.on_screen)

    # -- going away -------------------------------------------------------
    def test_hiding_takes_the_notifications_and_leaves_the_capsule(
            self) -> None:
        stub = Stub(hidden=False, up=("window", "caption_window",
                                      "card_window", "panel_window",
                                      "chevron_window"))
        self.hide(stub, True)
        self.assertEqual(stub.on_screen(), {"window", "caption_window"})
        self.assertEqual(stub.resized, 0, "the capsule is not touched")

    def test_hiding_keeps_every_flag(self) -> None:
        """The promise: hidden is not dismissed.

        The layout arithmetic reads these (the panel sits above the reply
        card only if there is one), and coming back has to restore what was
        there. Clearing them would silently swallow a notification.
        """
        stub = Stub(hidden=False, up=("card_window", "panel_window"))
        self.hide(stub, True)
        self.assertEqual(stub.flags(), {"card_visible", "panel_visible"})

    # -- coming back ------------------------------------------------------
    def test_coming_back_restores_what_was_there(self) -> None:
        stub = Stub(hidden=True, up=("card_window", "panel_window",
                                     "chevron_window"))
        self.hide(stub, False)
        self.assertEqual(stub.on_screen(),
                         {"card_window", "panel_window", "chevron_window"})

    def test_coming_back_shows_nothing_that_was_not_there(self) -> None:
        """A card the user dismissed stays dismissed."""
        stub = Stub(hidden=True, up=("panel_window",))
        self.hide(stub, False)
        self.assertEqual(stub.on_screen(), {"panel_window"})

    def test_empty_notifications_come_back_empty(self) -> None:
        stub = Stub(hidden=True)
        self.hide(stub, False)
        self.assertEqual(stub.on_screen(), set())

    # -- saying which way it went -----------------------------------------
    def test_the_agent_is_told(self) -> None:
        stub = Stub(hidden=True)
        self.hide(stub, False)
        self.assertEqual(self.reported, [{"event": "visibility",
                                          "hidden": False}])

    def test_setting_what_is_already_set_changes_nothing(self) -> None:
        stub = Stub(hidden=True, up=("card_window",))
        self.hide(stub, True)
        self.assertEqual(self.reported, [])
        self.assertEqual(stub.resized, 0)

    def test_it_survives_a_round_trip(self) -> None:
        stub = Stub(hidden=True, up=("window", "card_window",
                                     "chevron_window"))
        for _ in range(3):
            self.hide(stub, False)
            self.assertEqual(stub.on_screen(),
                             {"window", "card_window", "chevron_window"})
            self.hide(stub, True)
            self.assertEqual(stub.on_screen(), {"window"})


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class ProtocolTest(unittest.TestCase):
    """What the agent sends when the user double-taps Fn."""

    class Drainable(Stub):
        def __init__(self, hidden: bool = True) -> None:
            super().__init__(hidden)
            import threading
            from conductor.capsule_words import CapsuleWords
            self.lock = threading.Lock()
            self.pending: list[dict] = []
            # The capsule's words (boss.CAPSULE_WORDS) are drained with the
            # rest of the state, so the view carries a real line of them.
            self.view = type("V", (), {
                "state": "hidden", "text": "", "wave": [],
                "words": CapsuleWords(lambda word: 7.0 * len(word), 3.0)})()
            self.level = self.accum = self.peak = 0.0
            self.level_at = 0.0
            self.samples = 0
            self.said = []

        def set_hidden(self, hidden: bool) -> None:
            self.said.append(bool(hidden))
            self.hidden = bool(hidden)

    def drain(self, *messages, hidden: bool = True) -> list:
        stub = self.Drainable(hidden)
        stub.pending = list(messages)
        # _drain is a bare name and one argument, so pyobjc has published it
        # as a selector and will not take a plain Python object as self.
        # `.callable` is the function underneath it.
        overlay.Controller._drain.callable(stub)
        return stub.said

    def test_toggle_flips_it(self) -> None:
        self.assertEqual(self.drain({"toggle_hidden": True}, hidden=True),
                         [False])
        self.assertEqual(self.drain({"toggle_hidden": True}, hidden=False),
                         [True])

    def test_hidden_sets_it_outright(self) -> None:
        self.assertEqual(self.drain({"hidden": False}), [False])

    def test_an_ordinary_message_leaves_it_alone(self) -> None:
        self.assertEqual(self.drain({"state": "listening", "level": 0.4}), [])


@unittest.skipIf(voice_agent is None, "the audio stack is not installed")
class GestureReachesTheOverlayTest(unittest.TestCase):
    """The whole chain: a line from hotkey.py, out to the overlay.

    Each half is covered on its own - the gesture in tests.test_hotkey, the
    windows above - and the wiring between them is where a working toggle
    can still end up doing nothing at all.
    """

    class FakeLoop:
        """Runs what is handed to it, in the order it was handed over.

        The real one is an asyncio loop on another thread; what matters
        here is the order, because the key's own bookkeeping has to be
        queued before the gesture that rode in on it.
        """

        def call_soon_threadsafe(self, fn, *args) -> None:
            fn(*args)

    class FakeAgent:
        def __init__(self) -> None:
            self.keys, self.toggles = [], 0

        def key_changed(self, down: bool, double: bool = False) -> None:
            self.keys.append(("key", down, double))

        def toggle_overlay(self) -> None:
            self.toggles += 1
            self.keys.append(("toggle",))

    def read(self, *lines) -> "GestureReachesTheOverlayTest.FakeAgent":
        agent = self.FakeAgent()
        listener = voice_agent.HotkeyListener([], agent)
        listener.loop = self.FakeLoop()
        listener.proc = type("P", (), {
            "stdout": iter([line.encode() for line in lines])})()
        listener._read_events()
        return agent

    def test_a_flagged_release_toggles_the_notifications(self) -> None:
        agent = self.read('{"fn": true}\n',
                          '{"fn": false, "double": true}\n')
        self.assertEqual(agent.toggles, 1)

    def test_an_ordinary_hold_toggles_nothing(self) -> None:
        agent = self.read('{"fn": true}\n', '{"fn": false}\n')
        self.assertEqual(agent.toggles, 0)
        self.assertEqual(agent.keys, [("key", True, False),
                                      ("key", False, False)])

    def test_the_key_is_handled_before_the_gesture(self) -> None:
        """The release closes the microphone; the toggle is what that
        release additionally meant. Reversed, the notifications would
        appear while the utterance they belong to was still open."""
        agent = self.read('{"fn": false, "double": true}\n')
        self.assertEqual(agent.keys, [("key", False, True), ("toggle",)])

    def test_the_agent_asks_the_overlay_to_flip(self) -> None:
        """VoiceAgent does not track which way it went: the overlay owns
        the windows, so it owns the answer."""
        sent = []
        agent = object.__new__(voice_agent.VoiceAgent)
        agent.ui = type("U", (), {"send": lambda _s, **m: sent.append(m)})()
        agent.trace_id = ""
        voice_agent.VoiceAgent.toggle_overlay(agent)
        self.assertEqual(sent, [{"toggle_hidden": True}])


class SourceTest(unittest.TestCase):
    """No AppKit needed: the file itself has to hold the invariant."""

    def test_the_notifications_start_hidden(self) -> None:
        """The whole point. A default of False here is the old behaviour
        back, and every other test in this file would still pass."""
        init = self.method("init")
        self.assertTrue(
            any(isinstance(node, ast.Assign)
                and any(getattr(t, "attr", "") == "hidden" for t in node.targets)
                and getattr(node.value, "value", None) is True
                for node in ast.walk(init)),
            "Controller.init must set self.hidden = True")

    def test_the_capsule_keeps_animating_while_notifications_hide(
            self) -> None:
        """tick_ used to stop the capsule when the whole overlay hid. Only
        the notifications hide now, so nothing in tick_ may wait on it."""
        tick = self.method("tick_")
        for node in ast.walk(tick):
            if isinstance(node, ast.If) and isinstance(node.body[0],
                                                       ast.Return):
                names = {getattr(n, "attr", "") for n in ast.walk(node.test)}
                self.assertNotIn("hidden", names,
                                 "tick_ returns early on self.hidden")

    def test_nothing_puts_a_window_on_screen_behind_the_gate(self) -> None:
        """Every orderFront goes through _show_window, which is what makes
        hiding stick: one renderer calling it directly would put its window
        back over the user's work the next time it drew."""
        allowed = {"_show_window", "set_hidden"}
        tree = ast.parse(OVERLAY_SOURCE.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef) or func.name in allowed:
                continue
            for node in ast.walk(func):
                if (isinstance(node, ast.Attribute)
                        and node.attr == "orderFrontRegardless"):
                    self.fail(f"{func.name} (line {node.lineno}) puts a "
                              "window on screen without the visibility gate; "
                              "use self._show_window(...)")

    def method(self, name: str) -> ast.FunctionDef:
        tree = ast.parse(OVERLAY_SOURCE.read_text())
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef) and cls.name == "Controller":
                for node in cls.body:
                    if isinstance(node, ast.FunctionDef) and node.name == name:
                        return node
        self.fail(f"Controller.{name} is gone")


if __name__ == "__main__":
    unittest.main()
