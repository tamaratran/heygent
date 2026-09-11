"""Computer use: a worker the user allowed may drive this machine's GUI.

Each platform backend's grammar is tested against fakes (Quartz for
macOS, xdotool/scrot for X11, user32 for Windows), so all of it runs
anywhere; the real screen is exercised by tests/smoke_computer.py on a
Mac.

Run with:  python3 -m unittest tests.test_computer -v
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from conductor import computer, instance
from conductor.computer import (App, ComputerError, Driver, WindowsDriver,
                                X11Driver, main)
from conductor.conductor import SHARED_MACHINE_RULE, build_worker_prompt
from conductor.global_conductor import GlobalConductor
from conductor.manager import FakeManagerBackend
from conductor.runtime import ApprovalPolicy
from conductor.task_types import Task
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


class FakeQuartz:
    """Records every CGEvent the driver creates and posts."""

    kCGHIDEventTap = "hid-tap"
    kCGEventMouseMoved = "mouse-moved"
    kCGEventLeftMouseDown = "left-down"
    kCGEventLeftMouseUp = "left-up"
    kCGEventRightMouseDown = "right-down"
    kCGEventRightMouseUp = "right-up"
    kCGMouseButtonLeft = "btn-left"
    kCGMouseButtonRight = "btn-right"
    kCGMouseEventClickState = "click-state"
    kCGEventFlagMaskCommand = 1
    kCGEventFlagMaskShift = 2
    kCGEventFlagMaskAlternate = 4
    kCGEventFlagMaskControl = 8
    kCGEventFlagMaskSecondaryFn = 16

    def __init__(self) -> None:
        self.posted: list[dict] = []

    def CGPreflightScreenCaptureAccess(self) -> bool:
        return True

    def CGEventCreateMouseEvent(self, source, kind, point, button) -> dict:
        return {"kind": kind, "point": point, "button": button}

    def CGEventCreateKeyboardEvent(self, source, keycode, keydown) -> dict:
        return {"keycode": keycode, "keydown": keydown}

    def CGEventSetIntegerValueField(self, event, field, value) -> None:
        event[field] = value

    def CGEventKeyboardSetUnicodeString(self, event, length, text) -> None:
        event["text"] = text

    def CGEventSetFlags(self, event, flags) -> None:
        event["flags"] = flags

    def CGEventPost(self, tap, event) -> None:
        self.posted.append(event)


class FakeRun:
    """A subprocess.run stand-in that writes the 'screenshot'."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv, capture_output=False):
        self.calls.append(list(argv))
        if argv[0] == "screencapture" and self.returncode == 0:
            Path(argv[-1]).write_bytes(b"png")
        result = type("Result", (), {})()
        result.returncode = self.returncode
        result.stderr = b"not permitted" if self.returncode else b""
        return result


class FakeClock:
    """Time that passes only when the driver sleeps."""

    def __init__(self) -> None:
        self.now_s = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.now_s

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now_s += seconds


class FakeApps:
    """What NSWorkspace and the window list would say, made settable."""

    def __init__(self) -> None:
        self.apps = [App("Terminal", 10, "com.apple.Terminal"),
                     App("Google Chrome", 20, "com.google.Chrome"),
                     App("FaceTime", 30, "com.apple.FaceTime")]
        self.front_pid = 10
        self.window_counts = {10: 1, 20: 3}
        self.installed = {"messages": "com.apple.MobileSMS"}
        self.activated: list[int] = []

    def running(self) -> list[App]:
        return list(self.apps)

    def front(self) -> App | None:
        return next((a for a in self.apps if a.pid == self.front_pid), None)

    def bundle_for(self, name: str) -> str:
        return self.installed.get(name.lower(), "")

    def windows(self) -> dict[int, int]:
        return dict(self.window_counts)

    def activate(self, pid: int) -> bool:
        self.activated.append(pid)
        self.front_pid = pid
        return True


class FakeOpen:
    """`open` as Launch Services would carry it out, on a FakeApps."""

    def __init__(self, apps: FakeApps, windows: int = 1,
                 comes_forward: bool = False, returncode: int = 0,
                 starts: bool = True, then=None) -> None:
        self.apps = apps
        self.windows = windows
        self.comes_forward = comes_forward
        self.returncode = returncode
        self.starts = starts
        self.then = then
        self.calls: list[list[str]] = []

    def __call__(self, argv, capture_output=False):
        self.calls.append(list(argv))
        result = type("Result", (), {})()
        result.returncode = self.returncode
        result.stderr = (b"LSOpenURLsWithRole() failed with error -10810"
                         if self.returncode else b"")
        if self.returncode == 0 and self.starts:
            bundle = argv[-1]
            app = next((a for a in self.apps.apps if a.bundle == bundle),
                       None)
            if app is None:
                app = App("Messages", 40, bundle)
                self.apps.apps.append(app)
            if self.windows:
                self.apps.window_counts[app.pid] = self.windows
            if self.comes_forward or "-g" not in argv:
                self.apps.front_pid = app.pid
            if self.then is not None:
                self.then(self.apps)
        return result


class FakeClipboard:
    """The pasteboard, with a change count that moves the way macOS's does."""

    def __init__(self, items: list[dict] | None = None,
                 whole: bool = True) -> None:
        self.items = items if items is not None else [
            {"public.utf8-plain-text": b"what the user had copied"}]
        self.whole = whole
        self.count = 7
        self.puts: list[str] = []
        self.restored: list[list[dict]] = []

    def change_count(self) -> int:
        return self.count

    def snapshot(self) -> tuple[list[dict], bool]:
        return [dict(item) for item in self.items], self.whole

    def put_text(self, text: str) -> int:
        self.puts.append(text)
        self.count += 1
        return self.count

    def restore(self, items: list[dict]) -> None:
        self.restored.append(items)
        self.count += 1


def make_driver(ax: bool = True, run=None, secure: bool = False,
                apps: FakeApps | None = None,
                clipboard: FakeClipboard | None = None,
                clock: FakeClock | None = None
                ) -> tuple[Driver, FakeQuartz]:
    quartz = FakeQuartz()
    clock = clock or FakeClock()
    driver = Driver(quartz=quartz, run=run or FakeRun(), ax=lambda: ax,
                    pause=0, apps=apps or FakeApps(),
                    clipboard=clipboard or FakeClipboard(),
                    secure_input=lambda: secure, sleep=clock.sleep,
                    clock=clock.now)
    return driver, quartz


class DriverTest(unittest.TestCase):
    def test_a_click_moves_then_presses_at_the_point(self) -> None:
        driver, quartz = make_driver()
        driver.click(100, 200)
        kinds = [event["kind"] for event in quartz.posted]
        self.assertEqual(kinds, ["mouse-moved", "left-down", "left-up"])
        self.assertEqual(quartz.posted[1]["point"], (100.0, 200.0))

    def test_a_double_click_counts_its_presses(self) -> None:
        driver, quartz = make_driver()
        driver.click(10, 20, count=2)
        states = [event.get("click-state") for event in quartz.posted[1:]]
        self.assertEqual(states, [1, 1, 2, 2])

    def test_a_right_click_uses_the_right_button(self) -> None:
        driver, quartz = make_driver()
        driver.click(1, 2, button="right")
        self.assertEqual(quartz.posted[1]["kind"], "right-down")
        self.assertEqual(quartz.posted[1]["button"], "btn-right")

    def test_typing_carries_the_text_in_unicode_events(self) -> None:
        driver, quartz = make_driver()
        driver.type_text("hello")
        self.assertEqual([event["text"] for event in quartz.posted],
                         ["hello", "hello"])
        self.assertEqual([event["keydown"] for event in quartz.posted],
                         [True, False])

    def test_long_text_is_chunked(self) -> None:
        driver, quartz = make_driver()
        driver.type_text("x" * 45)
        texts = [event["text"] for event in quartz.posted
                 if event["keydown"]]
        self.assertEqual([len(text) for text in texts], [20, 20, 5])

    def test_a_chord_sets_modifier_flags_on_the_keycode(self) -> None:
        driver, quartz = make_driver()
        driver.press("cmd+shift+s")
        down, up = quartz.posted
        self.assertEqual(down["keycode"], 1)          # s
        self.assertEqual(down["flags"], 1 | 2)        # cmd + shift
        self.assertFalse(up["keydown"])

    def test_an_unknown_key_is_refused(self) -> None:
        driver, _ = make_driver()
        with self.assertRaises(ComputerError):
            driver.press("cmd+nonesuch")

    def test_input_refuses_without_accessibility(self) -> None:
        driver, quartz = make_driver(ax=False)
        for act in (lambda: driver.click(1, 2),
                    lambda: driver.type_text("hi"),
                    lambda: driver.press("cmd+s"),
                    lambda: driver.move(1, 2)):
            with self.assertRaises(ComputerError):
                act()
        self.assertEqual(quartz.posted, [])

    def test_a_screenshot_runs_screencapture(self) -> None:
        run = FakeRun()
        driver, _ = make_driver(run=run)
        with tempfile.TemporaryDirectory() as tmp:
            target = driver.screenshot(str(Path(tmp) / "shot.png"))
            self.assertTrue(target.exists())
        self.assertEqual(run.calls[0][:2], ["screencapture", "-x"])

    def test_a_failed_screenshot_says_why(self) -> None:
        driver, _ = make_driver(run=FakeRun(returncode=1))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ComputerError) as caught:
                driver.screenshot(str(Path(tmp) / "shot.png"))
        self.assertIn("not permitted", str(caught.exception))

    def test_permissions_reports_both(self) -> None:
        driver, _ = make_driver(ax=False)
        self.assertEqual(driver.permissions(),
                         {"accessibility": False, "screen_recording": True})


class FakeXdo:
    """A subprocess.run stand-in recording xdotool/scrot invocations."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv, capture_output=False, env=None):
        self.calls.append(list(argv))
        if argv[0] == "scrot" and self.returncode == 0:
            Path(argv[-1]).write_bytes(b"png")
        result = type("Result", (), {})()
        result.returncode = self.returncode
        result.stderr = b"no display" if self.returncode else b""
        return result


def make_x11(run: FakeXdo | None = None, display: str = ":0",
             tools: bool = True) -> tuple[X11Driver, FakeXdo]:
    run = run or FakeXdo()
    driver = X11Driver(run=run, env={"DISPLAY": display} if display else {},
                       which=(lambda name: f"/usr/bin/{name}") if tools
                       else (lambda name: None))
    return driver, run


class X11DriverTest(unittest.TestCase):
    def test_a_click_moves_then_clicks_the_button(self) -> None:
        driver, run = make_x11()
        driver.click(100, 200)
        self.assertEqual(run.calls, [
            ["xdotool", "mousemove", "100", "200"],
            ["xdotool", "click", "--repeat", "1", "1"]])

    def test_right_and_double_clicks_map_to_xdotool(self) -> None:
        driver, run = make_x11()
        driver.click(1, 2, button="right")
        driver.click(1, 2, count=2)
        self.assertEqual(run.calls[1][-1], "3")
        self.assertEqual(run.calls[3][2:], ["--repeat", "2", "1"])

    def test_typing_passes_the_text_literally(self) -> None:
        driver, run = make_x11()
        driver.type_text("héllo -x")
        self.assertEqual(run.calls[0][-2:], ["--", "héllo -x"])

    def test_a_chord_translates_cmd_to_ctrl(self) -> None:
        driver, run = make_x11()
        driver.press("cmd+shift+s")
        self.assertEqual(run.calls[0], ["xdotool", "key", "ctrl+shift+s"])

    def test_named_keys_become_keysyms(self) -> None:
        driver, run = make_x11()
        driver.press("return")
        self.assertEqual(run.calls[0][-1], "Return")

    def test_unknown_keys_and_modifiers_are_refused(self) -> None:
        driver, _ = make_x11()
        with self.assertRaises(ComputerError):
            driver.press("cmd+nonesuch")
        with self.assertRaises(ComputerError):
            driver.press("hyper+s")

    def test_input_refuses_without_a_display(self) -> None:
        driver, run = make_x11(display="")
        with self.assertRaises(ComputerError):
            driver.click(1, 2)
        self.assertEqual(run.calls, [])

    def test_a_screenshot_runs_scrot(self) -> None:
        driver, run = make_x11()
        with tempfile.TemporaryDirectory() as tmp:
            target = driver.screenshot(str(Path(tmp) / "shot.png"))
            self.assertTrue(target.exists())
        self.assertEqual(run.calls[0][0], "scrot")

    def test_a_failed_xdotool_says_why(self) -> None:
        driver, _ = make_x11(run=FakeXdo(returncode=1))
        with self.assertRaises(ComputerError) as caught:
            driver.move(1, 2)
        self.assertIn("no display", str(caught.exception))

    def test_permissions_report_the_missing_pieces(self) -> None:
        driver, _ = make_x11(tools=False)
        self.assertEqual(list(driver.permissions().values()),
                         [True, False, False])


class FakeUser32:
    """Records every user32 call the Windows backend makes."""

    def __init__(self, send_result: int = 1) -> None:
        self.send_result = send_result
        self.calls: list[tuple] = []

    def SetCursorPos(self, x, y):
        self.calls.append(("SetCursorPos", x, y))
        return 1

    def mouse_event(self, flags, dx, dy, data, extra):
        self.calls.append(("mouse_event", flags))

    def keybd_event(self, vk, scan, flags, extra):
        self.calls.append(("keybd_event", vk, flags))

    def SendInput(self, count, struct, size):
        self.calls.append(("SendInput", count))
        return self.send_result


class WindowsDriverTest(unittest.TestCase):
    def make(self, **kwargs) -> tuple[WindowsDriver, FakeUser32]:
        user32 = FakeUser32(**kwargs)
        return WindowsDriver(user32=user32, run=FakeRun(), pause=0), user32

    def test_a_click_positions_then_presses(self) -> None:
        driver, user32 = self.make()
        driver.click(100, 200)
        self.assertEqual(user32.calls, [
            ("SetCursorPos", 100, 200),
            ("mouse_event", 0x0002), ("mouse_event", 0x0004)])

    def test_a_right_double_click_uses_right_flags_twice(self) -> None:
        driver, user32 = self.make()
        driver.click(1, 2, button="right", count=2)
        flags = [call[1] for call in user32.calls[1:]]
        self.assertEqual(flags, [0x0008, 0x0010, 0x0008, 0x0010])

    def test_typing_sends_one_unicode_input_per_edge(self) -> None:
        driver, user32 = self.make()
        driver.type_text("hi")
        sends = [call for call in user32.calls if call[0] == "SendInput"]
        self.assertEqual(len(sends), 4)          # 2 chars x down+up

    def test_a_rejected_keystroke_is_an_error(self) -> None:
        driver, _ = self.make(send_result=0)
        with self.assertRaises(ComputerError):
            driver.type_text("x")

    def test_a_chord_holds_modifiers_around_the_key(self) -> None:
        driver, user32 = self.make()
        driver.press("cmd+s")
        self.assertEqual(user32.calls, [
            ("keybd_event", 0x11, 0),            # ctrl down (cmd -> ctrl)
            ("keybd_event", ord("S"), 0),
            ("keybd_event", ord("S"), 0x0002),
            ("keybd_event", 0x11, 0x0002)])

    def test_unknown_keys_are_refused(self) -> None:
        driver, _ = self.make()
        with self.assertRaises(ComputerError):
            driver.press("nonesuch")

    def test_a_screenshot_runs_powershell(self) -> None:
        run = FakeXdo()
        driver = WindowsDriver(user32=FakeUser32(), run=run, pause=0)
        with tempfile.TemporaryDirectory() as tmp:
            driver.screenshot(str(Path(tmp) / "shot.png"))
        self.assertEqual(run.calls[0][0], "powershell")
        self.assertIn("CopyFromScreen", run.calls[0][-1])


class MakeDriverTest(unittest.TestCase):
    def test_each_platform_gets_its_backend(self) -> None:
        self.assertIsInstance(computer.make_driver("linux"), X11Driver)
        with self.assertRaises(ComputerError):
            computer.make_driver("sunos5")


class CliTest(unittest.TestCase):
    """The command grammar, with the screen guard out of the way.

    The guard has its own module (tests/test_two_agents_one_keyboard):
    here the question is only whether `click 10 20 --double` becomes the
    right events, and a guard that reads the real screen would answer a
    different question on every machine.
    """

    def run_cli(self, argv, ax=True, **driver_args):
        driver, quartz = make_driver(ax=ax, **driver_args)
        lines: list[str] = []
        # A worker's own shell names its task, and the lease then answers
        # for that task. The grammar is the question here, not the lease.
        with mock.patch.dict(os.environ):
            os.environ.pop(instance.TASK_ENV, None)
            code = main(argv, driver_factory=lambda: driver,
                        out=lines.append, guard_factory=lambda: None)
        return code, lines, quartz

    def test_check_reports_permissions(self) -> None:
        code, lines, _ = self.run_cli(["check"])
        self.assertEqual(code, 0)
        self.assertIn("accessibility: granted", lines)

    def test_check_fails_when_a_permission_is_missing(self) -> None:
        code, lines, _ = self.run_cli(["check"], ax=False)
        self.assertEqual(code, 1)
        self.assertIn("accessibility: NOT granted", lines)

    def test_click_parses_coordinates_and_flags(self) -> None:
        code, _, quartz = self.run_cli(["click", "10", "20", "--double"])
        self.assertEqual(code, 0)
        self.assertEqual(len(quartz.posted), 5)       # move + 2 presses

    def test_a_refusal_is_an_exit_code_and_a_reason(self) -> None:
        code, lines, _ = self.run_cli(["type", "hi"], ax=False)
        self.assertEqual(code, 2)
        self.assertIn("Accessibility", lines[0])

    def test_apps_prints_the_listing(self) -> None:
        code, lines, _ = self.run_cli(["apps"])
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("front: Terminal"), lines)

    def test_open_says_what_is_now_true(self) -> None:
        apps = FakeApps()
        code, lines, _ = self.run_cli(["open", "Messages"], apps=apps,
                                      run=FakeOpen(apps))
        self.assertEqual(code, 0)
        self.assertIn("Messages is open in the background", lines[0])

    def test_paste_says_whether_the_clipboard_came_back(self) -> None:
        code, lines, _ = self.run_cli(["paste", "hi"])
        self.assertEqual(code, 0)
        self.assertEqual(
            lines, ["pasted 2 characters; the clipboard is back as it was"])

    def test_secure_input_is_a_warning_and_the_keys_still_go(self) -> None:
        """The user wants a worker that is told, not one that is stopped:
        the keys are posted, and the warning says macOS probably threw
        them away."""
        code, lines, quartz = self.run_cli(["type", "hi"], secure=True)
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "typed 2 characters")
        self.assertTrue(lines[1].startswith("warning: macOS secure input"))
        self.assertTrue(quartz.posted)

    def test_the_new_verbs_are_macos_only(self) -> None:
        driver, run = make_x11()
        for argv in (["apps"], ["open", "Messages"], ["paste", "hi"]):
            lines: list[str] = []
            with mock.patch.dict(os.environ):
                os.environ.pop(instance.TASK_ENV, None)
                code = main(argv, driver_factory=lambda: driver,
                            out=lines.append, guard_factory=lambda: None)
            self.assertEqual(code, 2)
            self.assertIn("macOS-only", lines[0])
        self.assertEqual(run.calls, [])

    def test_a_worker_without_the_lease_opens_the_app_and_is_told(self):
        """Opening an app changes what is on the user's screen, so a
        worker no conductor supervises is told it has none - after the
        app is open, because nothing is refused."""
        apps = FakeApps()
        run = FakeOpen(apps)
        driver, _ = make_driver(apps=apps, run=run)
        lines: list[str] = []
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {instance.TASK_ENV: "task_gone",
                                             instance.HOME_ENV: home}):
            code = main(["open", "Messages"], driver_factory=lambda: driver,
                        out=lines.append, guard_factory=lambda: None)
        self.assertEqual(code, 0)
        self.assertEqual(run.calls,
                         [["open", "-g", "-b", "com.apple.MobileSMS"]])
        self.assertIn("Messages is open", lines[0])
        self.assertTrue(lines[-1].startswith("warning: "))
        self.assertIn("no conductor is running", lines[-1])

    def test_listing_apps_needs_no_lease(self) -> None:
        """It reads, like look: a worker told to stop can still see what
        is on screen in order to report it."""
        driver, _ = make_driver()
        lines: list[str] = []
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {instance.TASK_ENV: "task_gone",
                                             instance.HOME_ENV: home}):
            code = main(["apps"], driver_factory=lambda: driver,
                        out=lines.append, guard_factory=lambda: None)
        self.assertEqual(code, 0)


class OpenAppTest(unittest.TestCase):
    """`open` reaches an app by name, never by its Dock icon - on
    2026-09-09 a click one icon over turned on FaceTime's camera instead
    of opening Messages."""

    def make(self, **open_args):
        apps, clock = FakeApps(), FakeClock()
        run = FakeOpen(apps, **open_args)
        driver, _ = make_driver(run=run, apps=apps, clock=clock)
        return driver, apps, run, clock

    def test_an_app_opens_behind_the_users_window_by_bundle_id(self) -> None:
        driver, apps, run, _ = self.make()
        said = driver.open_app("Messages")
        self.assertEqual(run.calls,
                         [["open", "-g", "-b", "com.apple.MobileSMS"]])
        self.assertEqual(apps.front_pid, 10, "the user keeps their window")
        self.assertIn("in the background with a window on screen", said)
        self.assertIn("Terminal is in front", said)

    def test_front_brings_it_forward(self) -> None:
        driver, _, run, _ = self.make()
        said = driver.open_app("Messages", front=True)
        self.assertEqual(run.calls, [["open", "-b", "com.apple.MobileSMS"]])
        self.assertIn("open in front with a window on screen", said)

    def test_a_running_app_is_found_by_the_name_it_shows(self) -> None:
        driver, _, run, _ = self.make()
        driver.open_app("google chrome")
        self.assertEqual(run.calls[0][-1], "com.google.Chrome")

    def test_a_name_nothing_answers_to_is_refused_not_guessed(self) -> None:
        """"Chrome" is not an app's name - Google Chrome is. The refusal
        lists what is running so the next try can use the real name."""
        driver, _, run, _ = self.make()
        with self.assertRaises(ComputerError) as caught:
            driver.open_app("Chrome")
        self.assertIn("Google Chrome", str(caught.exception))
        self.assertEqual(run.calls, [], "nothing was opened on a guess")

    def test_an_app_that_comes_forward_anyway_gives_the_front_back(self):
        driver, apps, _, _ = self.make(comes_forward=True)
        said = driver.open_app("Messages")
        self.assertEqual(apps.activated, [10])
        self.assertEqual(apps.front_pid, 10)
        self.assertIn("Terminal was put back in front", said)

    def test_a_front_that_cannot_be_given_back_is_said_plainly(self):
        driver, apps, _, _ = self.make(comes_forward=True)
        apps.activate = lambda pid: False
        said = driver.open_app("Messages")
        self.assertIn("could not be put back", said)

    def test_a_user_who_switched_apps_meanwhile_is_left_alone(self):
        """Only the app we opened is ever pushed back. If the user moved
        to another app themselves, that is where they stay."""
        driver, apps, _, _ = self.make(
            then=lambda apps: setattr(apps, "front_pid", 20))
        driver.open_app("Messages")
        self.assertEqual(apps.activated, [])
        self.assertEqual(apps.front_pid, 20)

    def test_an_app_with_no_window_says_so_after_waiting(self) -> None:
        driver, _, _, clock = self.make(windows=0)
        said = driver.open_app("Messages")
        self.assertIn("no window on screen", said)
        self.assertGreaterEqual(clock.now_s, computer.OPEN_WAIT_S)

    def test_a_refused_open_says_why(self) -> None:
        driver, _, _, _ = self.make(returncode=1)
        with self.assertRaises(ComputerError) as caught:
            driver.open_app("Messages")
        self.assertIn("-10810", str(caught.exception))

    def test_an_app_that_never_starts_is_an_error(self) -> None:
        driver, _, _, _ = self.make(starts=False)
        with self.assertRaises(ComputerError) as caught:
            driver.open_app("Messages")
        self.assertIn("is not running", str(caught.exception))


class AppsTest(unittest.TestCase):
    def test_the_front_app_comes_first_and_windows_are_counted(self) -> None:
        driver, _ = make_driver()
        self.assertEqual(driver.running_apps(), [
            "front: Terminal (com.apple.Terminal) - keystrokes go to this app",
            "  Terminal (com.apple.Terminal) - window on screen",
            "  FaceTime (com.apple.FaceTime) - no window on screen",
            "  Google Chrome (com.google.Chrome) - window on screen"])


class SecureInputTest(unittest.TestCase):
    """While macOS secure input is on, posted keystrokes are thrown away
    and nothing says so; posted clicks still arrive."""

    def test_typing_pasting_and_chords_still_post_and_are_warned(self) -> None:
        """Warned, not stopped: the keys go, and the warning says macOS
        probably threw them away."""
        clipboard = FakeClipboard()
        driver, quartz = make_driver(secure=True, clipboard=clipboard)
        driver.type_text("hello")
        driver.press("cmd+v")
        driver.paste_text("hello")
        self.assertEqual(len(quartz.posted), 6)
        self.assertEqual(clipboard.puts, ["hello"])
        warning = driver.keyboard_warning()
        self.assertIn("secure input", warning)
        self.assertIn("probably thrown away", warning)

    def test_the_warning_points_at_the_front_app(self) -> None:
        driver, _ = make_driver(secure=True)
        self.assertIn("The front app is Terminal", driver.keyboard_warning())

    def test_clicks_and_moves_still_go_through(self) -> None:
        driver, quartz = make_driver(secure=True)
        driver.click(1, 2)
        driver.move(3, 4)
        self.assertEqual(len(quartz.posted), 4)

    def test_a_probe_that_cannot_load_does_not_block_typing(self) -> None:
        with mock.patch("ctypes.util.find_library",
                        return_value="/no/such/Carbon"):
            self.assertFalse(computer._secure_input_on())


class PasteTest(unittest.TestCase):
    def test_the_text_goes_on_the_clipboard_and_one_cmd_v_is_pressed(self):
        clipboard = FakeClipboard()
        driver, quartz = make_driver(clipboard=clipboard)
        driver.paste_text("see you at eight")
        self.assertEqual(clipboard.puts, ["see you at eight"])
        down, up = quartz.posted
        self.assertEqual((down["keycode"], down["flags"]), (9, 1))  # cmd+v
        self.assertFalse(up["keydown"])

    def test_the_users_clipboard_comes_back_whole(self) -> None:
        """Not only its text: a copied picture or file goes back too."""
        saved = [{"public.png": b"\x89PNG", "public.utf8-plain-text": b"cap"},
                 {"public.file-url": b"file:///Users/x/a.pdf"}]
        clipboard = FakeClipboard(items=saved)
        driver, _ = make_driver(clipboard=clipboard)
        self.assertEqual(driver.paste_text("hi"), (True, True))
        self.assertEqual(clipboard.restored, [saved])

    def test_the_app_gets_time_to_read_it_before_it_goes_back(self) -> None:
        clock = FakeClock()
        driver, _ = make_driver(clock=clock)
        driver.paste_text("hi")
        self.assertIn(computer.PASTE_SETTLE_S, clock.slept)

    def test_a_clipboard_written_to_meanwhile_is_left_alone(self) -> None:
        """The user copied something while the paste landed. Putting back
        what they had before would throw away what they just copied."""
        clipboard, clock = FakeClipboard(), FakeClock()

        def sleep(seconds: float) -> None:
            clock.sleep(seconds)
            clipboard.count += 1

        driver = Driver(quartz=FakeQuartz(), run=FakeRun(), ax=lambda: True,
                        pause=0, apps=FakeApps(), clipboard=clipboard,
                        secure_input=lambda: False, sleep=sleep,
                        clock=clock.now)
        self.assertEqual(driver.paste_text("hi"), (False, True))
        self.assertEqual(clipboard.restored, [])

    def test_the_clipboard_goes_back_even_when_the_key_fails(self) -> None:
        clipboard = FakeClipboard()
        driver, quartz = make_driver(clipboard=clipboard)

        def broken(tap, event):
            raise RuntimeError("the window server went away")

        quartz.CGEventPost = broken
        with self.assertRaises(RuntimeError):
            driver.paste_text("hi")
        self.assertEqual(len(clipboard.restored), 1)

    def test_a_clipboard_that_could_not_all_be_read_is_reported(self) -> None:
        driver, _ = make_driver(clipboard=FakeClipboard(whole=False))
        self.assertEqual(driver.paste_text("hi"), (True, False))

    def test_nothing_to_paste_is_refused(self) -> None:
        driver, quartz = make_driver()
        with self.assertRaises(ComputerError):
            driver.paste_text("")
        self.assertEqual(quartz.posted, [])


REPO = Path(__file__).resolve().parent.parent
UV = "/opt/somewhere/uv"


def inline_dependencies(script: Path) -> list[str]:
    """The PEP 723 `# /// script` block's dependencies, as uv reads them."""
    match = re.search(r"^# /// script$\n(?P<body>(?:^#(?:| .*)$\n)+)^# ///$",
                      script.read_text(), re.MULTILINE)
    assert match, f"{script.name} declares no inline metadata"
    body = "\n".join(line[2:] if line.startswith("# ") else line[1:]
                     for line in match.group("body").splitlines())
    return tomllib.loads(body)["dependencies"]


class CliCommandTest(unittest.TestCase):
    """The worker is handed the driver on uv's managed runtime, so the
    Quartz bindings come with the script rather than with whichever
    python3 its shell finds."""

    script = str(REPO / "conductor" / "computer.py")

    def test_runs_the_script_on_uvs_managed_python(self) -> None:
        with mock.patch("conductor.boss_helper.find_uv", return_value=UV):
            command = computer.cli_command()
        self.assertTrue(command.startswith(f'"{UV}" run '), command)
        for flag in ("--python-preference only-managed", "--python 3.13",
                     "--no-project", "--quiet", "--script"):
            self.assertIn(flag, command)
        self.assertTrue(command.endswith(f"--script {self.script}"), command)

    def test_falls_back_to_python3_without_uv(self) -> None:
        with mock.patch("conductor.boss_helper.find_uv", return_value=None):
            self.assertEqual(computer.cli_command(),
                             f"python3 {self.script}")

    def test_the_brief_carries_the_same_command(self) -> None:
        with mock.patch("conductor.boss_helper.find_uv", return_value=UV):
            self.assertIn(computer.cli_command(), computer.worker_brief())

    def test_the_brief_sends_workers_to_open_rather_than_the_dock(self):
        brief = computer.worker_brief()
        for words in (" apps ", "open APP [--front]", "paste TEXT",
                      "never by clicking its Dock icon", "secure input"):
            self.assertIn(words, brief)


class BundledBindingsTest(unittest.TestCase):
    """pyobjc is part of the install, not a manual step: declared where
    uv resolves everything else, for the conductor's probe and for the
    script a worker runs."""

    def assert_declares_quartz(self, script: Path) -> None:
        names = [dep.split(";")[0].split(">")[0].split("<")[0].strip()
                 for dep in inline_dependencies(script)]
        self.assertIn("pyobjc-framework-Quartz", names)
        self.assertIn("pyobjc-framework-ApplicationServices", names)
        for dep in inline_dependencies(script):
            if dep.startswith("pyobjc"):
                self.assertIn("sys_platform == 'darwin'", dep)

    def test_the_conductor_declares_the_bindings(self) -> None:
        self.assert_declares_quartz(REPO / "conduct.py")

    def test_the_driver_script_declares_the_bindings(self) -> None:
        self.assert_declares_quartz(REPO / "conductor" / "computer.py")


class PolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ApprovalPolicy()
        self.cli = computer.cli_command()

    def decide(self, suffix: str) -> str:
        return self.policy.decide("Bash", {"command": f"{self.cli} {suffix}"})

    def test_looking_at_the_screen_is_allowed(self) -> None:
        self.assertEqual(self.decide("look"), "allow")
        self.assertEqual(self.decide("check"), "allow")

    def test_acting_on_the_screen_asks(self) -> None:
        self.assertEqual(self.decide("click 10 20"), "ask")
        self.assertEqual(self.decide("type 'hello'"), "ask")
        self.assertEqual(self.decide("key cmd+q"), "ask")

    def test_listing_apps_is_allowed(self) -> None:
        self.assertEqual(self.decide("apps"), "allow")

    def test_opening_an_app_and_pasting_ask(self) -> None:
        self.assertEqual(self.decide("open Messages"), "ask")
        self.assertEqual(self.decide("open Messages --front"), "ask")
        self.assertEqual(self.decide("paste 'see you at eight'"), "ask")

    def test_the_uv_form_classifies_by_its_verb(self) -> None:
        """The launcher's own flags never read as the verb, and its
        `run` never trips the install markers."""
        with mock.patch("conductor.boss_helper.find_uv", return_value=UV):
            cli = computer.cli_command()
        decide = lambda suffix: self.policy.decide(
            "Bash", {"command": f"{cli} {suffix}"})
        self.assertEqual(decide("look"), "allow")
        self.assertEqual(decide("look /tmp/a.png"), "allow")
        self.assertEqual(decide("check"), "allow")
        self.assertEqual(decide("click 10 20"), "ask")
        self.assertEqual(decide("key cmd+s"), "ask")


class WorkerBriefTest(unittest.TestCase):
    def make_task(self, computer_allowed: bool) -> Task:
        return Task(id="task_1", title="t", goal="g",
                    computer=computer_allowed)

    def test_an_ordinary_task_keeps_the_shared_machine_rule(self) -> None:
        prompt = build_worker_prompt(self.make_task(False), "")
        self.assertIn(SHARED_MACHINE_RULE, prompt)
        self.assertNotIn("operate this machine's GUI", prompt)

    def test_a_computer_task_gets_the_driver_commands(self) -> None:
        prompt = build_worker_prompt(self.make_task(True), "")
        self.assertNotIn(SHARED_MACHINE_RULE, prompt)
        self.assertIn(computer.cli_command(), prompt)
        self.assertIn("never play audio", prompt)

    def test_the_flag_survives_persistence(self) -> None:
        task = self.make_task(True)
        self.assertTrue(Task.from_dict(task.to_dict()).computer)
        self.assertFalse(Task.from_dict(
            self.make_task(False).to_dict()).computer)


class CreateTaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        roots = base / "code"
        (roots / "posely" / ".git").mkdir(parents=True)
        self.runtime = FakeCodingAgentRuntime()
        self.conductor = GlobalConductor(
            home=base / "home", runtime=self.runtime,
            manager=FakeManagerBackend(), search_roots=[roots],
            workspace_factory=lambda project: FakeWorkspaceManager())
        # The machine running this suite may or may not hold the grants;
        # the probe is patched so these tests mean the same everywhere.
        patcher = mock.patch("conductor.global_conductor.computer_state",
                             return_value=(True, ""))
        self.computer_state = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_flag_reaches_the_worker_brief(self) -> None:
        task = asyncio.run(self.conductor.handle_action(
            "create_task", {"project": "posely", "title": "Click it",
                            "goal": "click", "computer": True}))
        self.assertTrue(task.computer)
        (_, _, _, prompt) = self.runtime.calls[0]
        self.assertIn(computer.cli_command(), prompt)

    def test_the_probe_runs_the_moment_the_task_is_asked_for(self) -> None:
        """The snapshot taken at launch must never veto a grant made
        after it: the check happens at create_task, freshly."""
        asyncio.run(self.conductor.handle_action(
            "create_task", {"project": "posely", "title": "Click it",
                            "goal": "click", "computer": True}))
        self.computer_state.assert_called_once_with()

    def test_missing_grants_fail_with_where_to_turn_them_on(self) -> None:
        self.computer_state.return_value = (
            False, "needs Screen Recording - grant Accessibility and "
                   "Screen Recording in System Settings > Privacy & "
                   "Security to the terminal app that runs the workers "
                   "and the conductor, then ask again")
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(self.conductor.handle_action(
                "create_task", {"project": "posely", "title": "Click it",
                                "goal": "click", "computer": True}))
        self.assertIn("System Settings > Privacy & Security",
                      str(caught.exception))
        self.assertIn("then ask again", str(caught.exception))
        self.assertEqual(self.runtime.calls, [])

    def test_an_ordinary_task_never_probes_the_screen(self) -> None:
        asyncio.run(self.conductor.handle_action(
            "create_task", {"project": "posely", "title": "Fix it",
                            "goal": "fix"}))
        self.computer_state.assert_not_called()

    def test_the_default_stays_hands_off(self) -> None:
        task = asyncio.run(self.conductor.handle_action(
            "create_task", {"project": "posely", "title": "Fix it",
                            "goal": "fix"}))
        self.assertFalse(task.computer)
        (_, _, _, prompt) = self.runtime.calls[0]
        self.assertIn(SHARED_MACHINE_RULE, prompt)


if __name__ == "__main__":
    unittest.main()
