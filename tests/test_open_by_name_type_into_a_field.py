"""Be told when a click, a keystroke or a paste missed - never stopped.

2026-09-09: a one-step text took five minutes and never landed. The
computer-use worker needed Messages, clicked the Dock by position, hit
FaceTime, and the camera came on. It typed "Michelle Tran" into Messages'
search box while the box did not have the keyboard focus, and nothing
registered. It never got the thread open.

`open` (tests/test_computer.OpenAppTest) reaches an app by name. What
this file covers is the rest: the moves that miss SAY so - a click that
landed on the Dock, characters sent to no field or the wrong one, and
keystrokes macOS discards under secure input. Said, not stopped: the
action still happens and the warning comes back in its output, because a
worker that is told can look and put it right. Ported from intent-pilot's
fork of this driver, whose log also showed that of 308 actions 85% were
pixel clicks and the accessibility press was used zero times though the
prompt asked for it twice - so the quiet press is what a click IS.

Run with:  python3 -m unittest tests.test_open_by_name_type_into_a_field -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from conductor import computer, instance
from conductor.runtime import ApprovalPolicy
from conductor.screen_guard import AX_NO_VALUE, Element, Guard, MacEyes
from tests.test_computer import FakeClipboard, make_driver
from tests.test_two_agents_one_keyboard import FakeEyes

MESSAGES = ("Messages", "com.apple.MobileSMS")


def in_messages(**fields) -> Element:
    return Element(pid=2, app=MESSAGES[0], bundle_id=MESSAGES[1], **fields)


SEARCH = in_messages(role="AXTextField", subrole="AXSearchField",
                     label="Search", text_entry=True)
COMPOSE = in_messages(role="AXTextArea", label="iMessage", text_entry=True)
CONVERSATIONS = in_messages(role="AXTable", label="Conversations")
SEND = in_messages(role="AXButton", label="Send", actions=("AXPress",),
                   ref="send-button")
FACETIME_IN_THE_DOCK = Element(pid=40, app="Dock", bundle_id="com.apple.dock",
                               role="AXDockItem",
                               subrole="AXApplicationDockItem",
                               label="FaceTime",
                               actions=("AXPress", "AXShowMenu"))


class SecureInputIsReported(unittest.TestCase):
    def test_keystrokes_and_pastes_are_still_posted_and_warned_about(self):
        clipboard = FakeClipboard()
        driver, quartz = make_driver(secure=True, clipboard=clipboard)
        driver.type_text("Michelle Tran")
        driver.press("return")
        driver.paste_text("see you at eight")
        self.assertEqual(len(quartz.posted), 6, "nothing was held back")
        self.assertEqual(clipboard.puts, ["see you at eight"])
        self.assertIn("secure input is on", driver.keyboard_warning())

    def test_no_warning_while_it_is_off(self):
        driver, _ = make_driver()
        self.assertEqual(driver.keyboard_warning(), "")

    def test_the_warning_points_at_the_front_app_and_claims_no_holder(self):
        """The console session's secure-input pid named the FRONT app, not
        the process holding secure input (measured 2026-09-10): the front
        app is offered as the likeliest place, and nothing more."""
        driver, _ = make_driver(secure=True)
        warning = driver.keyboard_warning()
        self.assertIn("The front app is Terminal", warning)
        self.assertNotIn("held by", warning)


class AimingTest(unittest.TestCase):
    """Messages in front, the caret already in it, and a fresh look."""

    def setUp(self) -> None:
        self.eyes = FakeEyes()
        self.eyes.stack = list(reversed(self.eyes.stack))
        self.eyes.focused = (2, "AXTextField")
        self.guard = Guard(tempfile.mkdtemp(), eyes=self.eyes,
                           now=lambda: 1000.0)
        self.guard.record("/tmp/shot.png")


class TypingIsToldWhereTheCaretWas(AimingTest):
    def test_typing_into_nothing_is_warned_about(self):
        """The search box did not have the focus, and a name typed at it
        went nowhere. Macs say nothing when that happens; the driver
        does."""
        self.eyes.focused_field = Element()
        _, warning = self.guard.typing_target()
        self.assertIn("nothing had the keyboard focus", warning)

    def test_typing_at_something_that_is_not_a_field_is_warned_about(self):
        self.eyes.focused_field = CONVERSATIONS
        _, warning = self.guard.typing_target()
        self.assertIn("not a text field", warning)
        self.assertIn("Table 'Conversations' in Messages", warning)

    def test_a_field_in_another_app_is_warned_about(self):
        self.eyes.focused_field = Element(
            pid=2, app="FaceTime", bundle_id="com.apple.FaceTime",
            role="AXTextField", text_entry=True)
        _, warning = self.guard.typing_target(expect="Messages")
        self.assertIn("meant for 'Messages'", warning)
        self.assertIn("FaceTime", warning)

    def test_the_wrong_field_in_the_right_app_is_warned_about(self):
        """A message body typed into the search box is as lost as one
        typed into nothing, and --into is how a worker says which."""
        self.eyes.focused_field = SEARCH
        _, warning = self.guard.typing_target(expect="Messages",
                                              into="imessage")
        self.assertIn("meant for the 'imessage' field", warning)
        self.assertIn("SearchField 'Search' in Messages", warning)

    def test_the_right_field_needs_no_warning(self):
        self.eyes.focused_field = COMPOSE
        self.assertEqual(self.guard.typing_target(
            expect="Messages", into="imessage"), (COMPOSE, ""))
        self.eyes.focused_field = SEARCH
        self.assertEqual(self.guard.typing_target(
            expect="com.apple.MobileSMS", into="search"), (SEARCH, ""))

    def test_a_focus_nobody_can_read_is_no_opinion(self):
        """An app that does not answer accessibility queries is not an app
        with nothing focused, and a warning there would cry wolf."""
        self.eyes.focused_field = None
        self.assertEqual(self.guard.typing_target(expect="Messages"),
                         (None, ""))


class ClickingIsToldWhatItHit(AimingTest):
    def test_the_dock_is_warned_about_and_open_is_offered(self):
        self.eyes.target_at = FACETIME_IN_THE_DOCK
        target, warning = self.guard.click_target(500, 880)
        self.assertEqual(target, FACETIME_IN_THE_DOCK)
        self.assertIn("that was the Dock", warning)
        self.assertIn("FaceTime", warning)
        self.assertIn("`open APP`", warning)

    def test_the_dock_is_named_even_when_messages_is_expected(self):
        self.eyes.target_at = FACETIME_IN_THE_DOCK
        _, warning = self.guard.click_target(500, 880, expect="Messages")
        self.assertIn("Dock", warning)

    def test_an_element_another_app_owns_is_warned_about(self):
        self.eyes.target_at = Element(pid=3, app="Control Center",
                                      role="AXButton", actions=("AXPress",))
        _, warning = self.guard.click_target(900, 100, expect="Messages")
        self.assertIn("meant for 'Messages'", warning)
        self.assertIn("Control Center", warning)

    def test_the_right_element_needs_no_warning(self):
        self.eyes.target_at = SEND
        self.assertEqual(self.guard.click_target(900, 100, expect="Messages"),
                         (SEND, ""))


class TheCliActsAndSaysWhatItHit(AimingTest):
    def setUp(self) -> None:
        super().setUp()
        # This suite may itself run inside a worker's shell, which names a
        # task the lease would warn about. The lease has its own tests.
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop(instance.TASK_ENV, None)
        self.presses: list = []
        self.press_result = 0
        self.opened: list[str] = []
        self.secure = False

    def run_cli(self, argv):
        driver, quartz = make_driver()
        driver.perform_press = lambda ref: (self.presses.append(ref)
                                           or self.press_result)
        driver.open_app = lambda app, front=False: (
            self.opened.append(app) or f"{app} is open in the background")
        driver.secure_input = lambda: self.secure
        lines: list[str] = []
        with mock.patch.object(computer, "SETTLE_S", 0):
            code = computer.main(argv, driver_factory=lambda: driver,
                                 out=lines.append,
                                 guard_factory=lambda: self.guard)
        return code, lines, quartz

    def test_a_button_is_pressed_without_moving_the_cursor(self):
        self.eyes.target_at = SEND
        code, lines, quartz = self.run_cli(
            ["click", "900", "100", "--expect", "Messages"])
        self.assertEqual(code, 0)
        self.assertEqual(self.presses, ["send-button"])
        self.assertEqual(quartz.posted, [], "no pointer event was posted")
        self.assertEqual(len(lines), 1)
        self.assertIn("without moving the cursor", lines[0])

    def test_a_field_gets_a_real_click_because_the_caret_needs_one(self):
        self.eyes.target_at = SEARCH
        code, lines, quartz = self.run_cli(["click", "900", "100"])
        self.assertEqual(code, 0)
        self.assertEqual(self.presses, [])
        self.assertTrue(quartz.posted)
        self.assertIn("SearchField 'Search' in Messages", lines[0])

    def test_a_press_that_is_refused_becomes_a_real_click(self):
        self.eyes.target_at = SEND
        self.press_result = -25206
        code, _, quartz = self.run_cli(["click", "900", "100"])
        self.assertEqual(code, 0)
        self.assertEqual(self.presses, ["send-button"])
        self.assertTrue(quartz.posted)

    def test_pointer_forces_a_real_click(self):
        self.eyes.target_at = SEND
        _, _, quartz = self.run_cli(["click", "900", "100", "--pointer"])
        self.assertEqual(self.presses, [])
        self.assertTrue(quartz.posted)

    def test_a_click_on_the_dock_happens_and_says_it_was_the_dock(self):
        self.eyes.target_at = FACETIME_IN_THE_DOCK
        code, lines, quartz = self.run_cli(["click", "500", "880"])
        self.assertEqual(code, 0, "warned, not stopped")
        self.assertTrue(quartz.posted)
        self.assertTrue(lines[-1].startswith("warning: that was the Dock"))
        self.assertIn("`open APP`", lines[-1])

    def test_typing_into_nothing_still_types_and_says_so(self):
        self.eyes.focused_field = Element()
        code, lines, quartz = self.run_cli(["type", "Michelle Tran"])
        self.assertEqual(code, 0, "warned, not stopped")
        self.assertTrue(quartz.posted)
        self.assertEqual(lines[0], "typed 13 characters")
        self.assertIn("warning: nothing had the keyboard focus", lines[1])

    def test_typing_into_the_field_names_it_and_warns_of_nothing(self):
        self.eyes.focused_field = SEARCH
        code, lines, quartz = self.run_cli(
            ["type", "Michelle Tran", "--expect", "Messages",
             "--into", "search"])
        self.assertEqual(code, 0)
        self.assertTrue(quartz.posted)
        self.assertEqual(lines, ["typed 13 characters into "
                                 "SearchField 'Search' in Messages"])

    def test_a_paste_names_the_field_it_went_into(self):
        self.eyes.focused_field = COMPOSE
        code, lines, _ = self.run_cli(
            ["paste", "running late", "--expect", "Messages",
             "--into", "imessage"])
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["pasted 12 characters into TextArea "
                                 "'iMessage' in Messages; the clipboard is "
                                 "back as it was"])

    def test_a_paste_into_the_wrong_field_still_pastes_and_says_so(self):
        self.eyes.focused_field = SEARCH
        code, lines, quartz = self.run_cli(
            ["paste", "running late", "--into", "imessage"])
        self.assertEqual(code, 0, "warned, not stopped")
        self.assertTrue(quartz.posted)
        self.assertIn("warning: this was meant for the 'imessage' field",
                      lines[-1])

    def test_keys_under_secure_input_are_sent_and_warned_about(self):
        self.eyes.focused_field = COMPOSE
        self.secure = True
        code, lines, quartz = self.run_cli(["key", "return"])
        self.assertEqual(code, 0, "warned, not stopped")
        self.assertTrue(quartz.posted)
        self.assertTrue(lines[-1].startswith(
            "warning: macOS secure input is on"))

    def test_open_needs_no_look(self):
        """Opening aims at no point and no caret, so the screen guard is
        not asked: somebody else's key press is the next click's news."""
        self.eyes.presses["key_down"] += 1
        code, lines, _ = self.run_cli(["open", "Messages"])
        self.assertEqual((code, self.opened), (0, ["Messages"]))
        self.assertEqual(lines, ["Messages is open in the background"])

    def test_open_without_a_conductor_still_opens_and_says_so(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {instance.TASK_ENV: "task_gone",
                                             instance.HOME_ENV: home}):
            code, lines, _ = self.run_cli(["open", "Messages"])
        self.assertEqual((code, self.opened), (0, ["Messages"]))
        self.assertTrue(lines[-1].startswith("warning: no conductor"))


class MacEyesReadTheElement(unittest.TestCase):
    """The real AX reading code, against a fake ApplicationServices."""

    class FakeAX:
        def __init__(self, attrs=None, names=(), actions=(), settable=False,
                     focus_error=0, system_error=None) -> None:
            self.attrs = attrs or {}
            self.names = tuple(names)
            self.actions = tuple(actions)
            self.settable = settable
            self.focus_error = focus_error          # asked of the front app
            self.system_error = focus_error if system_error is None \
                else system_error

        def AXUIElementCreateSystemWide(self):
            return "system-wide"

        def AXUIElementCreateApplication(self, pid):
            return "front-app"

        def AXUIElementCopyAttributeValue(self, element, name, _):
            if element in ("system-wide", "front-app"):
                err = self.system_error if element == "system-wide" \
                    else self.focus_error
                return err, "node" if err == 0 else None
            if name in self.attrs:
                return 0, self.attrs[name]
            return AX_NO_VALUE, None

        def AXUIElementCopyElementAtPosition(self, element, x, y, _):
            return 0, "node"

        def AXUIElementGetPid(self, node, _):
            return 0, 2

        def AXUIElementCopyAttributeNames(self, node, _):
            return 0, list(self.attrs) + list(self.names)

        def AXUIElementCopyActionNames(self, node, _):
            return 0, list(self.actions)

        def AXUIElementIsAttributeSettable(self, node, name, _):
            return 0, self.settable

    def eyes(self, front_pid=2, **kwargs) -> MacEyes:
        eyes = MacEyes(quartz=object(), ax=self.FakeAX(**kwargs))
        for name, value in (("app_of", MESSAGES), ("front_pid", front_pid)):
            patcher = mock.patch.object(MacEyes, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return eyes

    def read(self, **kwargs) -> Element | None:
        return self.eyes(**kwargs).focused_element()

    def test_the_focus_is_asked_of_the_app_in_front(self):
        """On this Mac the system-wide element answered -25204 for every
        app in front, while the front app's own element named its focused
        text area. Asked system-wide only, the typing check - and the
        focus-moved check before it - would never have fired."""
        caret = {"attrs": {"AXRole": "AXTextArea"}, "system_error": -25204}
        self.assertTrue(self.read(**caret).text_entry)
        self.assertEqual(self.eyes(**caret).focus(), (2, "AXTextArea"))

    def test_with_no_app_in_front_it_asks_system_wide(self):
        found = self.read(front_pid=0, attrs={"AXRole": "AXTextField"},
                          focus_error=-25204, system_error=0)
        self.assertTrue(found.text_entry)

    def test_a_search_field_is_a_place_to_type(self):
        found = self.read(attrs={"AXRole": "AXTextField",
                                 "AXSubrole": "AXSearchField",
                                 "AXPlaceholderValue": "Search"})
        self.assertTrue(found.text_entry)
        self.assertEqual(found.describe(), "SearchField 'Search' in Messages")

    def test_a_button_is_pressable_and_no_place_to_type(self):
        found = self.read(attrs={"AXRole": "AXButton", "AXTitle": "Send"},
                          actions=("AXPress",))
        self.assertTrue(found.pressable)
        self.assertFalse(found.text_entry)

    def test_a_row_is_left_to_a_real_click(self):
        """A row that takes AXPress may still not select, and a press that
        reports success while doing nothing is the worst kind of click."""
        found = self.read(attrs={"AXRole": "AXRow"}, actions=("AXPress",))
        self.assertFalse(found.pressable)

    def test_a_custom_text_view_is_a_place_to_type(self):
        caret = {"attrs": {"AXRole": "AXGroup"},
                 "names": ("AXSelectedTextRange",)}
        self.assertTrue(self.read(settable=True, **caret).text_entry)
        self.assertFalse(self.read(settable=False, **caret).text_entry)

    def test_a_dock_icon_is_known_for_what_it_is(self):
        eyes = MacEyes(quartz=object(), ax=self.FakeAX(
            attrs={"AXRole": "AXDockItem", "AXTitle": "FaceTime"},
            actions=("AXPress",)))
        with mock.patch.object(MacEyes, "app_of",
                               return_value=("Dock", "com.apple.dock")):
            self.assertTrue(eyes.element_under(500, 880).in_dock)

    def test_nothing_focused_is_an_answer(self):
        self.assertEqual(self.read(focus_error=AX_NO_VALUE), Element())

    def test_an_app_that_does_not_answer_is_no_answer(self):
        self.assertIsNone(self.read(focus_error=-25204))


class TheWorkerIsTold(unittest.TestCase):
    def test_the_brief_names_the_warnings_and_the_quiet_click(self):
        brief = computer.worker_brief()
        self.assertIn("never by clicking its Dock icon", brief)
        self.assertIn("`warning:` line", brief)
        self.assertIn("Nothing you do is refused", brief)
        self.assertIn("--into", brief)
        self.assertIn("--pointer", brief)
        self.assertNotIn("exit code", brief)

    def test_pasting_asks_like_every_other_action(self):
        command = f"{computer.cli_command()} paste 'running late'"
        self.assertEqual(ApprovalPolicy().decide("Bash", {"command": command}),
                         "ask")


if __name__ == "__main__":
    unittest.main()
