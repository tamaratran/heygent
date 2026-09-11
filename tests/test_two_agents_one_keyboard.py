"""We are not the only thing driving this screen.

2026-09-08: ChatGPT's computer-use agent was running on this machine at
the same time as our worker. A "?" reached a contact nobody meant to
message, and a word said out loud to the Boss was typed into a Messages
compose box. Two agents, one keyboard, and no shared notion of whose turn
it is - because macOS offers none. Nothing announces "I have the input";
nothing arbitrates between automation clients; and there is no
point-in-time question that answers "was the last event synthetic" (the
HID and combined event-source states look like the answer and are not:
posting to kCGHIDEventTap advances both, measured 2026-09-09).

What IS reliable is the negative: whether the screen is still what our
screenshot showed. So look, then act, and be told when anything moved
under you - told, not stopped: the action still happens and a warning
comes back with it. See conductor/screen_guard.py.

Run with:  python3 -m unittest tests.test_two_agents_one_keyboard -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import computer, instance
from conductor.screen_guard import Guard, Window


class FakeEyes:
    """What macOS would say, made settable.

    counters and idle move the way a second hand on the keyboard moves
    them; windows and focus the way another app coming forward does.
    """

    def __init__(self) -> None:
        self.presses = {"key_down": 100, "left_click": 50, "right_click": 5}
        self.idle = 30.0
        self.stack = [Window(pid=1, number=11, app="Chrome",
                             title="Inbox", bounds=(0, 0, 800, 600)),
                      Window(pid=2, number=22, app="Messages",
                             title="Michelle", bounds=(800, 0, 400, 600))]
        self.focused = (1, "AXTextField")
        self.owner_at = None            # None: whoever owns the window
        self.posters = set()
        # What typing and clicking aim at, in detail. None is macOS having
        # no answer, which the guard reads as no opinion.
        self.focused_field = None
        self.target_at = None

    def counters(self):
        return dict(self.presses)

    def seconds_since_input(self):
        return self.idle

    def windows(self):
        return tuple(self.stack)

    def focus(self):
        return self.focused

    def element_at(self, x, y):
        if self.owner_at is not None:
            return self.owner_at, "AXButton"
        window = next((w for w in self.stack if w.contains(x, y)), None)
        return (window.pid if window else 0), "AXButton"

    def focused_element(self):
        return self.focused_field

    def element_under(self, x, y):
        return self.target_at

    def posting_processes(self, seconds=0.0):
        return set(self.posters)


class GuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.eyes = FakeEyes()
        self.clock = [1000.0]
        self.guard = Guard(tempfile.mkdtemp(), eyes=self.eyes,
                           now=lambda: self.clock[0])
        self.guard.record("/tmp/shot.png")

    def warns(self, *args, **kwargs) -> str:
        warnings = self.guard.check(*args, **kwargs)
        self.assertTrue(warnings, "expected a warning")
        return "\n".join(warnings)

    def quiet(self, *args, **kwargs) -> None:
        self.assertEqual(self.guard.check(*args, **kwargs), [])


class NothingChangedUnderUs(GuardTest):
    def test_a_click_on_a_screen_that_has_not_moved_says_nothing(self):
        self.quiet("click", point=(100, 100), target="point")

    def test_typing_with_the_caret_where_we_left_it_says_nothing(self):
        self.quiet("type", target="focus")


class SomebodyElseUsedTheKeyboard(GuardTest):
    def test_a_key_press_we_did_not_make_is_reported(self):
        """The counters cannot say WHO - no macOS API can, outside a
        running event tap - but they say it was not us, which is what the
        worker needs to hear."""
        self.eyes.presses["key_down"] += 1
        self.assertIn("1 key press happened",
                      self.warns("type", target="focus"))

    def test_a_click_we_did_not_make_is_reported(self):
        self.eyes.presses["left_click"] += 3
        self.assertIn("3 clicks happened",
                      self.warns("click", point=(100, 100), target="point"))

    def test_the_warning_names_the_process_when_one_is_still_posting(self):
        """The one place macOS attributes an event is a listen-only tap,
        whose kCGEventSourceUnixProcessID is 0 for a hand on the keyboard
        and the poster's pid for a program. It costs a third of a second,
        so it is spent explaining a warning already decided - never on
        the path of an action nothing is wrong with."""
        self.eyes.presses["key_down"] += 1
        self.eyes.posters = {4242}
        self.assertIn("4242", self.warns("type", target="focus"))

    def test_input_happening_right_now_is_reported(self):
        """Between our look and our act is milliseconds; a foreign agent
        mid-action will often not have moved a counter yet. That it
        touched the input a moment ago is the other half of the check."""
        self.clock[0] += 5
        self.eyes.idle = 0.2
        self.assertIn("was not us",
                      self.warns("click", point=(100, 100), target="point"))

    def test_our_own_last_action_is_not_read_as_somebody_else(self):
        """Our click is input the system saw a fraction of a second ago,
        and the sighting that follows it is taken right after. Without
        this, the second click of any pair warns about the first."""
        self.eyes.idle = 0.05
        self.guard.record()
        self.quiet("click", point=(100, 100), target="point")

    def test_moving_the_mouse_is_not_somebody_acting(self):
        """The user's hand is on this machine and the cursor moves all
        day. A warning that fired on that is one a worker learns to
        ignore."""
        self.eyes.idle = 30.0
        self.quiet("click", point=(100, 100), target="point")


class TheTargetMoved(GuardTest):
    def test_a_new_front_window_is_reported(self):
        self.eyes.stack = list(reversed(self.eyes.stack))
        self.assertIn("front window changed",
                      self.warns("click", point=(100, 100), target="point"))

    def test_a_different_window_under_the_point_is_reported(self):
        """The exact shape of the damage: the coordinates came from a
        screenshot, and by the time they were used they pointed at
        somebody else's window."""
        self.eyes.stack[1] = Window(pid=9, number=99, app="Slack",
                                    title="general", bounds=(800, 0, 400, 600))
        message = self.warns("click", point=(900, 100), target="point")
        self.assertIn("not what the screenshot showed", message)
        self.assertIn("Slack", message)

    def test_a_window_that_no_longer_covers_the_point_is_reported(self):
        self.eyes.stack[1] = Window(pid=2, number=22, app="Messages",
                                    title="Michelle", bounds=(0, 900, 10, 10))
        self.assertIn("is now nothing",
                      self.warns("click", point=(900, 100), target="point"))

    def test_an_element_owned_by_another_process_is_reported(self):
        """A sheet, a popover or another agent's window can sit over the
        one the listing still puts there. What is actually under the
        cursor is the AX element, and it names its own process."""
        self.eyes.owner_at = 77
        self.assertIn("process 77",
                      self.warns("click", point=(100, 100), target="point"))

    def test_moved_focus_is_reported_for_typing(self):
        """Typing goes where the caret is, so for typing the caret IS the
        target: a word meant for one window lands in whichever one has
        it."""
        self.eyes.focused = (2, "AXTextArea")
        self.assertIn("keyboard focus moved to another process",
                      self.warns("type", target="focus"))

    def test_moved_focus_says_nothing_about_a_click(self):
        """A click names its own target. A warning about a caret that
        moved would be noise nobody could work with."""
        self.eyes.focused = (2, "AXTextArea")
        self.quiet("click", point=(100, 100), target="point")

    def test_the_app_a_worker_names_is_checked(self):
        message = self.warns("click", point=(100, 100), target="point",
                             expect="Safari")
        self.assertIn("meant for 'Safari'", message)

    def test_naming_the_right_app_says_nothing(self):
        self.quiet("click", point=(100, 100), target="point", expect="chrome")


class LookingAgainClearsTheWarning(GuardTest):
    def test_acting_with_no_look_at_all_is_reported(self):
        guard = Guard(tempfile.mkdtemp(), eyes=self.eyes,
                      now=lambda: self.clock[0])
        warnings = guard.check("click", point=(1, 1), target="point")
        self.assertIn("nothing had looked at this screen", warnings[0])

    def test_an_old_look_is_reported(self):
        self.clock[0] += 3600
        self.assertIn("3600s before this click",
                      self.warns("click", point=(100, 100), target="point"))

    def test_looking_again_makes_a_changed_screen_quiet(self):
        """Looking again is what a warning asks for - the step that was
        skipped when a worker wrote over somebody else."""
        self.eyes.stack = list(reversed(self.eyes.stack))
        self.warns("click", point=(100, 100), target="point")
        self.guard.record("/tmp/shot2.png")
        self.quiet("click", point=(100, 100), target="point")

    def test_our_own_action_becomes_the_new_baseline(self):
        """Otherwise the second click in a row reads our own first click
        as somebody else's."""
        self.eyes.presses["left_click"] += 1
        self.guard.record()
        self.quiet("click", point=(100, 100), target="point")


class TheDriverActsAndSays(unittest.TestCase):
    """The CLI seam: every acting verb goes through permit(), and what it
    finds is printed after the action as a `warning:` line. Nothing is
    refused: the worker is told, not stopped."""

    def setUp(self) -> None:
        self.eyes = FakeEyes()
        self.guard = Guard(tempfile.mkdtemp(), eyes=self.eyes,
                           now=lambda: 1000.0)
        self.guard.record("/tmp/shot.png")
        self.home = Path(tempfile.mkdtemp())
        # This suite may itself run inside a worker's shell, whose task the
        # lease would warn about. The lease has its own test below.
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop(instance.TASK_ENV, None)

    def run_cli(self, argv):
        from tests.test_computer import make_driver
        driver, quartz = make_driver()
        lines: list[str] = []
        with mock.patch.object(computer, "SETTLE_S", 0):
            code = computer.main(argv, driver_factory=lambda: driver,
                                 out=lines.append,
                                 guard_factory=lambda: self.guard)
        return code, lines, quartz

    def test_a_click_on_an_unchanged_screen_goes_through_quietly(self):
        code, lines, quartz = self.run_cli(["click", "100", "100"])
        self.assertEqual(code, 0)
        self.assertTrue(quartz.posted)
        self.assertEqual(lines, ["clicked 100,100"])

    def test_a_click_on_a_changed_screen_still_happens_and_says_why(self):
        self.eyes.stack[0] = Window(pid=9, number=99, app="Slack",
                                    title="general", bounds=(0, 0, 800, 600))
        code, lines, quartz = self.run_cli(["click", "100", "100"])
        self.assertEqual(code, 0, "warned, not stopped")
        self.assertTrue(quartz.posted)
        self.assertEqual(lines[0], "clicked 100,100")
        self.assertTrue(lines[1].startswith("warning: "))
        self.assertIn("Slack", lines[1])

    def test_look_records_the_baseline_it_is_read_against(self):
        guard = Guard(tempfile.mkdtemp(), eyes=self.eyes,
                      now=lambda: 1000.0)
        self.assertIsNone(guard.last())
        from tests.test_computer import make_driver
        driver, _ = make_driver()
        driver.run = lambda *a, **k: type("R", (), {"returncode": 0})()
        computer.main(["look", "/tmp/shot.png"],
                      driver_factory=lambda: driver, out=lambda s: None,
                      guard_factory=lambda: guard)
        self.assertEqual(guard.last().screenshot, "/tmp/shot.png")

    def test_a_worker_with_no_conductor_behind_it_is_told(self):
        """The lease and the guard are two different questions, and both
        are answered: a worker left behind by a restart still acts, and is
        told it was left behind so it can say so."""
        environ = {instance.TASK_ENV: "task_gone",
                   instance.HOME_ENV: str(self.home)}
        held = instance.lease(None, environ=environ)
        self.assertFalse(held)
        target, warnings = computer.permit(
            "click", point=(1, 1), guard=self.guard,
            lease=lambda *a, **k: held)
        self.assertEqual(len(warnings), 1)
        self.assertIn("no conductor is running", warnings[0])

    def test_a_paste_with_the_caret_in_another_app_is_warned_about(self):
        """A paste lands where the caret is, exactly like typing, so it
        is checked the same way - and, like typing, still happens."""
        from tests.test_computer import FakeClipboard, make_driver
        clipboard = FakeClipboard()
        driver, quartz = make_driver(clipboard=clipboard)
        self.eyes.focused = (2, "AXTextArea")
        lines: list[str] = []
        with mock.patch.object(computer, "SETTLE_S", 0):
            code = computer.main(["paste", "see you soon"],
                                 driver_factory=lambda: driver,
                                 out=lines.append,
                                 guard_factory=lambda: self.guard)
        self.assertEqual(code, 0, "warned, not stopped")
        self.assertEqual(clipboard.puts, ["see you soon"])
        self.assertTrue(quartz.posted)
        self.assertTrue(lines[0].startswith("pasted 12 characters"), lines)
        self.assertIn("warning: the keyboard focus moved", lines[1])


if __name__ == "__main__":
    unittest.main()
