"""The permissions computer use needs are asked for at start, once each.

macOS never asks for Accessibility or Screen Recording itself; a worker
without them just fails silently. So the conductor opens the right
Privacy & Security pane as it starts and says which apps to add - once
per missing grant, not on every launch, and never when everything is
granted or off macOS. The pane is a fake here: these tests must not
open System Settings on the machine running them.

Run with:  python3 -m unittest tests.test_gui_permissions -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import gui_permissions
from conductor.computer import ComputerError
from conductor.gui_permissions import (PANES, STATE_FILE, Ask,
                                       ask_for_missing_grants, this_app)

ROOT = Path(__file__).resolve().parent.parent
GHOSTTY = {"__CFBundleIdentifier": "com.mitchellh.ghostty",
           "TERM_PROGRAM": "ghostty"}


class FakeDriver:
    def __init__(self, accessibility=True, screen_recording=True) -> None:
        self.granted = {"accessibility": accessibility,
                        "screen_recording": screen_recording}

    def permissions(self) -> dict[str, bool]:
        return dict(self.granted)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.opened: list[str] = []
        self.said: list[str] = []
        self.registered: list[str] = []
        self.driver = FakeDriver()
        self.voice = {"microphone": True, "input_monitoring": True}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def ask(self, platform="darwin", worker_app="cmux", env=GHOSTTY,
            opener=None, grants=None, dialog=None) -> list[Ask]:
        return ask_for_missing_grants(
            self.home, worker_app=worker_app, platform=platform, env=env,
            driver_factory=lambda: self.driver,
            voice_probe=lambda wanted: {name: self.voice.get(name)
                                        for name in wanted},
            grants=grants,
            opener=opener or (lambda url: self.opened.append(url) or True),
            announce=self.said.append,
            register=self.registered.append, dialog=dialog)

    def remembered(self) -> dict:
        path = self.home / STATE_FILE
        return json.loads(path.read_text())["asked"] if path.exists() else {}


class AskingOnce(Base):
    def test_everything_granted_introduces_and_opens_nothing(self) -> None:
        self.assertEqual(self.ask(), [])
        self.assertEqual(self.opened, [])
        self.assertEqual(len(self.said), 1, "the first-launch banner alone")
        self.assertIn("First launch", self.said[0])
        self.assertEqual(self.ask(), [])
        self.assertEqual(len(self.said), 1, "introduced on every launch")

    def test_a_missing_grant_opens_its_pane_and_names_the_apps(self) -> None:
        self.driver = FakeDriver(screen_recording=False)
        asks = self.ask()
        self.assertEqual([a.grant for a in asks], ["screen_recording"])
        self.assertEqual(self.opened, [PANES["screen_recording"]])
        self.assertEqual(len(self.said), 2, "the banner, then the ask")
        message = self.said[-1]
        self.assertIn("Screen Recording", message)
        self.assertIn("Ghostty", message)
        self.assertIn("cmux", message)
        self.assertIn("Privacy & Security", message)
        self.assertNotIn("Accessibility", message,
                         "only the grant that is missing")

    def test_both_missing_opens_both_panes(self) -> None:
        self.driver = FakeDriver(False, False)
        asks = self.ask()
        self.assertEqual([a.grant for a in asks],
                         ["accessibility", "screen_recording"])
        self.assertEqual(self.opened,
                         [PANES["accessibility"], PANES["screen_recording"]])

    def test_the_second_launch_stays_quiet(self) -> None:
        self.driver = FakeDriver(accessibility=False)
        self.ask()
        self.ask()
        self.ask()
        self.assertEqual(len(self.opened), 1, "asked on every launch")
        self.assertEqual(len(self.said), 2, "the banner, then the ask")
        self.assertEqual(set(self.remembered()), {"accessibility"})

    def test_each_grant_is_asked_for_on_its_own(self) -> None:
        """Accessibility asked yesterday does not silence Screen Recording
        found missing today."""
        self.driver = FakeDriver(accessibility=False)
        self.ask()
        self.driver = FakeDriver(accessibility=False, screen_recording=False)
        asks = self.ask()
        self.assertEqual([a.grant for a in asks], ["screen_recording"])
        self.assertEqual(self.opened[-1], PANES["screen_recording"])

    def test_a_grant_that_arrives_is_forgotten_so_a_revocation_asks_again(
            self) -> None:
        self.driver = FakeDriver(screen_recording=False)
        self.ask()
        self.driver = FakeDriver()
        self.assertEqual(self.ask(), [])
        self.assertEqual(self.remembered(), {}, "still remembered")
        self.driver = FakeDriver(screen_recording=False)
        self.assertEqual([a.grant for a in self.ask()], ["screen_recording"])
        self.assertEqual(len(self.opened), 2)

    def test_a_pane_that_would_not_open_is_still_explained(self) -> None:
        self.driver = FakeDriver(accessibility=False)
        asks = self.ask(opener=lambda url: False)
        self.assertFalse(asks[0].opened)
        self.assertIn("Open System Settings", self.said[-1])
        self.assertEqual(set(self.remembered()), {"accessibility"},
                         "a failed open is still one ask")

    def test_an_unreadable_memory_is_an_empty_one(self) -> None:
        self.home.mkdir(parents=True)
        (self.home / STATE_FILE).write_text("not json")
        self.driver = FakeDriver(accessibility=False)
        self.assertEqual(len(self.ask()), 1)
        self.assertEqual(set(self.remembered()), {"accessibility"})


class StayingHarmless(Base):
    def test_other_platforms_are_left_alone(self) -> None:
        self.driver = FakeDriver(False, False)
        for platform in ("linux", "win32"):
            self.assertEqual(self.ask(platform=platform), [])
        self.assertEqual(self.opened, [])
        self.assertEqual(self.said, [])

    def test_no_bindings_means_nothing_to_ask_for(self) -> None:
        """A pane cannot fix a missing pyobjc; the capability probe
        reports that one."""
        def no_driver():
            raise ComputerError("computer use needs macOS Quartz (pyobjc)")
        asks = ask_for_missing_grants(
            self.home, worker_app="cmux", platform="darwin", env=GHOSTTY,
            driver_factory=no_driver,
            voice_probe=lambda wanted: {name: None for name in wanted},
            opener=lambda url: self.opened.append(url) or True,
            announce=self.said.append, register=self.registered.append)
        self.assertEqual(asks, [])
        self.assertEqual(self.opened, [])

    def test_the_real_opener_is_never_the_default_in_these_tests(self) -> None:
        """A guard on the guard: the module's default opener runs `open`
        and its default register puts up macOS's own prompt, which this
        suite must never reach."""
        self.assertIs(ask_for_missing_grants.__defaults__, None)
        self.assertEqual(
            ask_for_missing_grants.__kwdefaults__["opener"],
            gui_permissions.open_pane)
        self.assertEqual(
            ask_for_missing_grants.__kwdefaults__["register"],
            gui_permissions.register_with_macos)


class ScreenRecordingIsAskedForWhenItMatters(Base):
    """v0.3.6 on a fresh Mac: heygent logged "asked for Screen Recording"
    and never appeared under Screen & System Audio Recording. The request
    came from a process with no window-server connection and never
    reached tccd. Measured with two fresh bundle ids: the plain call
    logged no request; after NSApplication.sharedApplication() it logged
    "Notifying for access kTCCServiceScreenCapture" for the app."""

    def test_launch_does_not_ask_for_screen_recording(self) -> None:
        self.driver = FakeDriver(screen_recording=False)
        asks = self.ask(grants=gui_permissions.LAUNCH_GRANTS)
        self.assertEqual(asks, [])
        self.assertNotIn("screen_recording", self.registered)
        self.assertNotIn(PANES["screen_recording"], self.opened)

    def test_launch_still_asks_for_the_rest(self) -> None:
        self.driver = FakeDriver(accessibility=False, screen_recording=False)
        self.voice["input_monitoring"] = False
        asks = self.ask(grants=gui_permissions.LAUNCH_GRANTS)
        self.assertEqual([a.grant for a in asks],
                         ["input_monitoring", "accessibility"])

    def test_conduct_asks_for_the_launch_grants(self) -> None:
        source = (ROOT / "conduct.py").read_text()
        self.assertIn("grants=None if worker_app else LAUNCH_GRANTS", source)

    def test_with_cmux_workers_launch_still_names_cmux_for_the_screen(
            self) -> None:
        """Devin Review on #37: cmux's Screen Recording grant cannot be
        probed from the conductor and was named only by the launch ask.
        With cmux hosting the workers, launch keeps asking for it."""
        self.driver = FakeDriver(screen_recording=False)
        asks = self.ask(worker_app="cmux", grants=None)
        self.assertEqual([a.grant for a in asks], ["screen_recording"])
        self.assertIn("cmux", self.said[-1])

    def test_the_request_connects_to_the_window_server_first(self) -> None:
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        gui_permissions._request_screen_capture(run=run)
        (argv,) = calls
        self.assertEqual(argv[:2], [sys.executable, "-c"])
        code = argv[2]
        self.assertLess(code.index("NSApplication.sharedApplication()"),
                        code.index("CGRequestScreenCaptureAccess()"))

    def test_a_request_that_fails_is_raised_to_be_logged(self) -> None:
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "",
                                               "ModuleNotFoundError: AppKit")
        with self.assertRaises(RuntimeError) as caught:
            gui_permissions._request_screen_capture(run=run)
        self.assertIn("AppKit", str(caught.exception))

    def test_registering_screen_recording_uses_the_connected_request(
            self) -> None:
        with mock.patch.object(gui_permissions,
                               "_request_screen_capture") as request:
            gui_permissions.register_with_macos("screen_recording")
        request.assert_called_once_with()

    @unittest.skipUnless(sys.platform == "darwin", "macOS bindings")
    def test_the_request_code_only_uses_what_conduct_installs(self) -> None:
        import importlib.util
        for module in ("AppKit", "Quartz"):
            self.assertIsNotNone(importlib.util.find_spec(module), module)
        source = (ROOT / "conduct.py").read_text()
        self.assertIn("pyobjc-framework-Cocoa", source)

    def test_a_computer_task_lists_the_app_in_each_missing_pane(self) -> None:
        self.driver = FakeDriver(screen_recording=False)
        asked = gui_permissions.register_missing_computer_grants(
            driver_factory=lambda: self.driver,
            register=self.registered.append, platform="darwin")
        self.assertEqual(asked, ["screen_recording"])
        self.assertEqual(self.registered, ["screen_recording"])

    def test_nothing_missing_asks_for_nothing(self) -> None:
        asked = gui_permissions.register_missing_computer_grants(
            driver_factory=lambda: self.driver,
            register=self.registered.append, platform="darwin")
        self.assertEqual((asked, self.registered), ([], []))

    def test_other_platforms_are_left_alone(self) -> None:
        self.driver = FakeDriver(screen_recording=False)
        asked = gui_permissions.register_missing_computer_grants(
            driver_factory=lambda: self.driver,
            register=self.registered.append, platform="linux")
        self.assertEqual((asked, self.registered), ([], []))

    def test_the_remedy_names_the_plus_button(self) -> None:
        from conductor.computer import Driver
        self.assertIn("click + and choose it from Applications",
                      Driver.remedy)


class NamingTheApp(unittest.TestCase):
    def test_the_bundle_id_names_the_terminal(self) -> None:
        self.assertEqual(this_app({"__CFBundleIdentifier": "com.cmuxterm.app",
                                   "TERM_PROGRAM": "ghostty"}), "cmux")
        self.assertEqual(this_app({"__CFBundleIdentifier": "com.apple.Terminal"}),
                         "Terminal")

    def test_term_program_stands_in_for_an_unknown_bundle(self) -> None:
        self.assertEqual(this_app({"TERM_PROGRAM": "iTerm.app"}), "iTerm2")
        self.assertEqual(this_app({"__CFBundleIdentifier": "org.example.NewTerm",
                                   "TERM_PROGRAM": "newterm"}), "newterm")
        self.assertEqual(this_app({"__CFBundleIdentifier": "org.example.NewTerm"}),
                         "NewTerm")

    def test_nothing_known_still_says_where_to_look(self) -> None:
        self.assertEqual(this_app({}), gui_permissions.UNKNOWN_APP)

    def test_the_worker_app_is_not_repeated_when_it_is_this_one(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        said: list[str] = []
        ask_for_missing_grants(
            Path(tmp.name), worker_app="cmux", platform="darwin",
            env={"__CFBundleIdentifier": "com.cmuxterm.app"},
            driver_factory=lambda: FakeDriver(accessibility=False),
            voice_probe=lambda wanted: {name: True for name in wanted},
            opener=lambda url: True, announce=said.append,
            register=lambda grant: None)
        tmp.cleanup()
        self.assertEqual(said[-1].count("cmux"), 2, said[-1])
        self.assertNotIn("cmux and cmux", said[-1])


class VoiceGrants(Base):
    """Microphone and Input Monitoring are asked for the same way, at the
    start, so the answer to \"why does it not hear me\" is on screen
    before the first hold of Fn."""

    def test_a_denied_microphone_opens_its_pane(self) -> None:
        self.voice["microphone"] = False
        asks = self.ask()
        self.assertEqual([a.grant for a in asks], ["microphone"])
        self.assertEqual(self.opened, [PANES["microphone"]])
        self.assertIn("Microphone", self.said[-1])
        self.assertIn("hear you", self.said[-1])
        self.assertNotIn("cmux", self.said[-1],
                         "the microphone is this process's grant alone")

    def test_input_monitoring_says_to_restart_the_terminal(self) -> None:
        self.voice["input_monitoring"] = False
        asks = self.ask()
        self.assertEqual([a.grant for a in asks], ["input_monitoring"])
        self.assertEqual(self.opened, [PANES["input_monitoring"]])
        self.assertIn("Input Monitoring", self.said[-1])
        self.assertIn("restart your terminal", self.said[-1])

    def test_an_unknowable_grant_is_not_a_missing_one(self) -> None:
        """None means macOS will ask on first use (or the bindings are
        absent); a pane opened for it would be noise."""
        self.voice = {"microphone": None, "input_monitoring": None}
        self.assertEqual(self.ask(), [])
        self.assertEqual(self.opened, [])

    def test_a_subset_asks_for_that_subset_alone(self) -> None:
        """A voice-only caller asks for the voice grants and leaves the
        computer-use panes (and their memory) untouched."""
        self.driver = FakeDriver(False, False)
        self.voice["microphone"] = False
        asks = self.ask(grants=("microphone", "input_monitoring"))
        self.assertEqual([a.grant for a in asks], ["microphone"])
        self.assertEqual(self.opened, [PANES["microphone"]])


class FromFinder(Base):
    """Opened as heygent.app there is no terminal: print goes to a log
    file, and what the user saw was System Settings opening on a list
    with no heygent in it and no word of what to do. So the ask is a
    dialog, the row is put in the list first, and the pane opens when
    they say so."""

    HEYGENT = {"__CFBundleIdentifier": "ai.heygent.conductor"}

    def dialog(self, answer):
        self.shown: list[tuple[str, tuple]] = []

        def show(text, buttons):
            self.shown.append((text, buttons))
            return answer
        return show

    def test_the_row_is_registered_before_the_pane_is_offered(self) -> None:
        self.driver = FakeDriver(accessibility=False)
        asks = self.ask(env=self.HEYGENT, worker_app=None,
                        dialog=self.dialog(gui_permissions.OPEN_SETTINGS))
        self.assertEqual(self.registered, ["accessibility"])
        self.assertEqual(self.opened, [PANES["accessibility"]])
        self.assertTrue(asks[0].opened)
        text, buttons = self.shown[0]
        self.assertEqual(buttons, (gui_permissions.LATER,
                                   gui_permissions.OPEN_SETTINGS))
        self.assertIn("switch next to heygent", text)
        self.assertIn("click + and choose heygent", text)
        self.assertIn("quit heygent and open it again", text)
        self.assertNotIn("conduct.sh", text)
        self.assertEqual(self.said[1:], [], "printed as well as shown")

    def test_later_leaves_settings_closed_and_does_not_nag(self) -> None:
        self.voice["input_monitoring"] = False
        asks = self.ask(env=self.HEYGENT,
                        dialog=self.dialog(gui_permissions.LATER))
        self.assertEqual(self.opened, [])
        self.assertFalse(asks[0].opened)
        self.assertIn("newly started apps", self.shown[0][0])
        self.assertEqual(self.ask(env=self.HEYGENT,
                                  dialog=self.dialog(gui_permissions.LATER)),
                         [], "asked again next launch")

    def test_the_row_is_registered_from_a_terminal_too(self) -> None:
        self.driver = FakeDriver(screen_recording=False)
        self.ask()
        self.assertEqual(self.registered, ["screen_recording"])
        self.assertIn("restart conduct.sh", self.said[-1])

    def test_conduct_shows_the_dialog_only_without_a_terminal(self) -> None:
        source = (ROOT / "conduct.py").read_text()
        self.assertIn("dialog=None if dialogs.has_terminal() else "
                      "dialogs.alert", source)


class FirstLaunchBanner(Base):
    """The very first launch introduces the permissions before any pane
    opens: what each grant is for and when it matters."""

    def test_the_banner_names_all_four_grants_and_their_purposes(self) -> None:
        banner = gui_permissions.first_launch_banner()
        self.assertIn("Privacy & Security", banner)
        for label in ("Microphone", "Input Monitoring",
                      "Accessibility", "Screen Recording"):
            self.assertIn(label, banner)
        self.assertIn("needed always", banner)
        self.assertIn("only for computer-use tasks", banner)
        self.assertIn("restart your terminal", banner)

    def test_the_banner_is_plain_unless_asked_to_dress_up(self) -> None:
        plain = gui_permissions.first_launch_banner()
        self.assertNotIn("\x1b[", plain)
        dressed = gui_permissions.first_launch_banner(color=True)
        self.assertIn("\x1b[", dressed)
        for line in dressed.splitlines():
            self.assertTrue(line.endswith(gui_permissions._STYLES["reset"])
                            or "\u2502" not in line, line)

    def test_the_dressed_banner_links_each_grant_to_its_pane(self) -> None:
        dressed = gui_permissions.first_launch_banner(color=True)
        for pane in PANES.values():
            self.assertIn(f"\x1b]8;;{pane}\x1b\\", dressed)
        plain = gui_permissions.first_launch_banner()
        self.assertNotIn("\x1b]8", plain)

    def test_the_banner_comes_before_the_asks(self) -> None:
        self.driver = FakeDriver(False, False)
        self.ask()
        self.assertIn("First launch", self.said[0])
        self.assertIn("Accessibility", self.said[1])

    def test_a_voice_subset_shows_the_voice_rows_alone(self) -> None:
        self.ask(grants=("microphone", "input_monitoring"))
        banner = self.said[0]
        self.assertIn("Microphone", banner)
        self.assertIn("Input Monitoring", banner)
        self.assertNotIn("Accessibility", banner)
        self.assertNotIn("Screen Recording", banner)

    def test_new_grants_are_introduced_even_after_a_subset_was(self) -> None:
        """A voice-only set first, the full set later: the wider caller
        still gets its banner; a repeat of the same set stays quiet."""
        self.ask(grants=("microphone", "input_monitoring"))
        self.ask()
        self.assertEqual(len(self.said), 2, "one banner per new set")
        self.assertIn("Screen Recording", self.said[1])
        self.ask()
        self.assertEqual(len(self.said), 2, "all introduced already")


class AskedAtTheStart(unittest.TestCase):
    """The user's words: it should do that at the start. Before the cmux
    preflight, which can install and launch an app, not after."""

    def test_conduct_asks_before_the_surface_preflight(self) -> None:
        source = (ROOT / "conduct.py").read_text()
        ask = source.index("ask_for_missing_grants, home")
        preflight = source.index("await preflight_surface(")
        self.assertLess(ask, preflight)

    def test_conduct_asks_off_the_loop(self) -> None:
        source = (ROOT / "conduct.py").read_text()
        self.assertIn("asyncio.to_thread(\n        ask_for_missing_grants",
                      source)


class ADeniedMicrophoneIsARefusal(unittest.TestCase):
    """A Mac where the Microphone switch is off looks alive and hears
    nothing: the stream opens and delivers zeros. Asked for once like
    the other grants; after that, said as a refusal before the app
    quits, the way a refused Fn tap is."""

    def test_only_an_explicit_denial(self) -> None:
        from conductor.gui_permissions import microphone_denied
        self.assertTrue(microphone_denied(lambda w: {"microphone": False}))
        self.assertFalse(microphone_denied(lambda w: {"microphone": True}))
        self.assertFalse(microphone_denied(lambda w: {"microphone": None}))
        self.assertFalse(microphone_denied(lambda w: {}))

    def test_conduct_refuses_after_the_ask_and_releases_the_instance(
            self) -> None:
        source = (ROOT / "conduct.py").read_text()
        ask = source.index("asks = await asyncio.to_thread(\n"
                           "        ask_for_missing_grants, home")
        refuse = source.index('explain_refusal, "microphone"')
        self.assertLess(ask, refuse)
        self.assertLess(refuse, source.index("await preflight_surface("))
        tail = source[refuse:refuse + 400]
        self.assertIn("instance.release(home, current_run())", tail)
        self.assertIn("return 1", tail)
        # The first denial was just asked for in a dialog of its own; a
        # second dialog on top of it says the same thing twice.
        self.assertIn('all(ask.grant != "microphone" for ask in asks)',
                      source)
