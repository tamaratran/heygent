#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pyobjc-framework-Quartz>=10,<13; sys_platform == 'darwin'",
#   "pyobjc-framework-ApplicationServices>=10,<13; sys_platform == 'darwin'",
#   "pyobjc-framework-Cocoa>=10,<13; sys_platform == 'darwin'",
# ]
# ///

"""Driving this machine's GUI, for a worker the user has allowed to.

A deterministic driver, not a model, with one backend per platform:
Quartz CGEvents and `screencapture` on macOS, xdotool and scrot on
Linux/X11, and user32 SendInput with a PowerShell capture on Windows. A
worker reaches it through Bash as a command-line tool - `look` and `apps`
to see the screen and what is running, `open` to reach an app by name,
`click`/`type`/`paste`/`key` to act on it - so every action passes the
same approval gate as any other command, and every action is one line in
the transcript the user can read back.

Each backend names what it needs: macOS the Accessibility and Screen
Recording permissions, X11 a display with the tools installed. `check`
reports it, and input commands refuse up front rather than posting
events the platform silently drops.

Run with:  uv run --script /path/to/conductor/computer.py <command> ...

The inline metadata above is the whole install: on macOS uv brings the
Quartz bindings (pyobjc) with the script, the same way conduct.sh brings
everything conduct.py needs, so nothing has to be pip-installed into
whichever `python3` a worker's shell happens to find. cli_command() is
the exact invocation a worker is handed.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from . import instance
    from .observability import application_log
    from .screen_guard import Guard, MacEyes
else:                                        # run as a script from a worker
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from conductor import instance
    from conductor.observability import application_log
    from conductor.screen_guard import Guard, MacEyes

SCREENSHOT_DIR = Path.home() / ".voice-conductor" / "screenshots"


def conductor_home() -> Path:
    """Where the lock and the guard's sighting live.

    A worker is launched with VOICE_CONDUCTOR_HOME set, so a conductor
    started with --home is still the one this driver answers to.
    """
    return Path(os.environ.get(instance.HOME_ENV)
                or instance.DEFAULT_HOME).expanduser()


# macOS virtual keycodes (Carbon Events.h), US layout. `type` covers
# arbitrary text via the unicode-string field; these are for chords.
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
    "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
    "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37,
    "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44,
    "n": 45, "m": 46, ".": 47, "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "backspace": 51, "escape": 53, "esc": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
}

MODIFIERS = {
    "cmd": "kCGEventFlagMaskCommand", "command": "kCGEventFlagMaskCommand",
    "shift": "kCGEventFlagMaskShift",
    "alt": "kCGEventFlagMaskAlternate", "option": "kCGEventFlagMaskAlternate",
    "ctrl": "kCGEventFlagMaskControl", "control": "kCGEventFlagMaskControl",
    "fn": "kCGEventFlagMaskSecondaryFn",
}


class ComputerError(RuntimeError):
    """The driver cannot act, and says why in one sentence."""


def _load_quartz():
    try:
        import Quartz
    except ImportError as exc:
        raise ComputerError(
            "computer use needs macOS Quartz (pyobjc)") from exc
    return Quartz


def _ax_trusted() -> bool:
    try:
        from ApplicationServices import AXIsProcessTrusted
    except ImportError:
        return False
    return bool(AXIsProcessTrusted())


def _ax_press(ref) -> int:
    """AXPress an element; the AX error code, 0 when it took the press."""
    try:
        from ApplicationServices import AXUIElementPerformAction
        return int(AXUIElementPerformAction(ref, "AXPress"))
    except (ImportError, TypeError, ValueError):
        return -1


# An app's window, for `apps` and `open`: an ordinary (layer 0) window at
# least this big on both sides. Helper processes keep 0x0 and 1x1 windows
# at that layer; a real window can be small while it is still opening -
# Calculator's first on-screen frame after `open -g` was 226x52
# (2026-09-10).
MIN_WINDOW_PT = 20

# How long `open` waits for the app to be running and to show a window.
# Calculator, launched in the background, was running 0.17s after `open`
# returned and had its window at 1.4s (2026-09-10).
OPEN_WAIT_S = 5.0

# How long the app gets to read the pasteboard after cmd+v before the
# user's own clipboard goes back: it reads the board while handling the
# key, not when the event is posted. intent-pilot's value, not measured
# here.
PASTE_SETTLE_S = 0.4


def _secure_input_on() -> bool:
    """Whether any process holds macOS secure event input right now.

    While one does - a password field, a terminal with Secure Keyboard
    Entry, some call windows - macOS throws posted keystrokes away and
    still delivers posted clicks, and nothing reports the loss. The
    answer is session-wide: with one python holding secure input, a
    second python asked and read True, then False once it was released
    (2026-09-10).
    """
    import ctypes
    import ctypes.util
    try:
        carbon = ctypes.CDLL(ctypes.util.find_library("Carbon"))
        probe = carbon.IsSecureEventInputEnabled
    except (OSError, AttributeError):
        # Not a reason to refuse typing - without Carbon there is no
        # secure input to be in - but written down, so a Mac where the
        # probe fails does not read as one where it is off.
        application_log("ui", "computer.secure_input_unknown",
                        "could not ask macOS whether secure input is on",
                        severity="warning", exc_info=True)
        return False
    probe.restype = ctypes.c_bool
    return bool(probe())


@dataclass(frozen=True)
class App:
    """A running app with a Dock icon."""
    name: str
    pid: int
    bundle: str = ""


class MacApps:
    """What macOS says about running apps and their windows.

    One injectable place for the AppKit calls, the way MacEyes is for the
    screen guard: the driver's decisions are tested against a fake, and
    this is the part only a Mac can answer.
    """

    def __init__(self, quartz=None) -> None:
        self.quartz = quartz

    @staticmethod
    def _workspace():
        """NSWorkspace, brought up to date first.

        Its app list and front app change only while a run loop runs, and
        this process never runs one. Read once before a launch, the list
        still lacked the launched app 4s later, while one pass of the run
        loop brought it in - and took it out again after a quit
        (2026-09-10). `open` reads the list before launching to resolve
        the name, so without the pass it waited out its whole timeout and
        reported a running app as not running.
        """
        try:
            from AppKit import (NSDate, NSDefaultRunLoopMode, NSRunLoop,
                                NSWorkspace)
        except ImportError as exc:
            raise ComputerError(
                "listing and opening apps needs macOS AppKit (pyobjc)"
            ) from exc
        NSRunLoop.currentRunLoop().runMode_beforeDate_(
            NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.01))
        return NSWorkspace.sharedWorkspace()

    @staticmethod
    def _app(running) -> App:
        return App(name=str(running.localizedName() or ""),
                   pid=int(running.processIdentifier()),
                   bundle=str(running.bundleIdentifier() or ""))

    def running(self) -> list[App]:
        # Regular apps only (activation policy 0), the ones with a Dock
        # icon. Agents, helpers and menu-bar extras are the rest.
        return [self._app(app) for app in
                self._workspace().runningApplications()
                if app.activationPolicy() == 0]

    def front(self) -> App | None:
        """The app keystrokes go to."""
        app = self._workspace().frontmostApplication()
        return self._app(app) if app is not None else None

    def bundle_for(self, name: str) -> str:
        """The bundle id Launch Services resolves a name to, or ''."""
        path = self._workspace().fullPathForApplication_(name)
        if not path:
            return ""
        try:
            with (Path(str(path)) / "Contents" / "Info.plist").open(
                    "rb") as handle:
                return str(plistlib.load(handle).get(
                    "CFBundleIdentifier") or "")
        except (OSError, plistlib.InvalidFileException, ValueError):
            return ""

    def windows(self) -> dict[int, int]:
        """How many windows each process has on screen."""
        counts: dict[int, int] = {}
        for window in MacEyes(quartz=self.quartz).windows():
            if len(window.bounds) == 4 \
                    and min(window.bounds[2:]) >= MIN_WINDOW_PT:
                counts[window.pid] = counts.get(window.pid, 0) + 1
        return counts

    def activate(self, pid: int) -> bool:
        """Bring a running app forward."""
        from AppKit import NSRunningApplication
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(
            pid)
        # 2 is NSApplicationActivateIgnoringOtherApps.
        return bool(app is not None and app.activateWithOptions_(2))


class MacClipboard:
    """The general pasteboard: set aside, written to, put back.

    Every item and every type on it is kept, not only its text - a user
    who had copied a picture must not get an empty clipboard back.
    """

    # Clipboard managers skip content carrying these (nspasteboard.org),
    # so what a worker pastes does not land in the user's history.
    TRANSIENT = ("org.nspasteboard.TransientType",
                 "org.nspasteboard.AutoGeneratedType")
    # NSPasteboardContentsCurrentHostOnly: not carried to the user's
    # other devices by Universal Clipboard.
    THIS_MAC_ONLY = 1

    def __init__(self) -> None:
        try:
            from AppKit import NSPasteboard
        except ImportError as exc:
            raise ComputerError("paste needs macOS AppKit (pyobjc)") from exc
        self.board = NSPasteboard.generalPasteboard()

    def change_count(self) -> int:
        return int(self.board.changeCount())

    def snapshot(self) -> tuple[list[dict], bool]:
        """Every item's data by type, and whether all of it was readable.

        A type an app only promised - data it would produce on demand -
        may have nothing to read now, and is the part that cannot come
        back.
        """
        items, whole = [], True
        for item in self.board.pasteboardItems() or ():
            kept = {}
            for kind in item.types() or ():
                data = item.dataForType_(kind)
                if data is None:
                    whole = False
                else:
                    kept[str(kind)] = data
            if kept:
                items.append(kept)
        return items, whole

    def put_text(self, text: str) -> int:
        """Replace the clipboard with text; the change count it leaves."""
        from AppKit import NSPasteboardTypeString
        self.board.prepareForNewContentsWithOptions_(self.THIS_MAC_ONLY)
        self.board.setString_forType_(text, NSPasteboardTypeString)
        for kind in self.TRANSIENT:
            self.board.setString_forType_("", kind)
        return self.change_count()

    def restore(self, items: list[dict]) -> None:
        from AppKit import NSPasteboardItem
        self.board.clearContents()
        restored = []
        for kept in items:
            item = NSPasteboardItem.alloc().init()
            for kind, data in kept.items():
                item.setData_forType_(data, kind)
            restored.append(item)
        if restored:
            self.board.writeObjects_(restored)


class Driver:
    """CGEvent-level control of the screen, cursor and keyboard (macOS).

    quartz, run, ax, apps, clipboard, secure_input, sleep, clock and
    perform_press are injectable so the event grammar and the decisions
    around it are testable off-Mac; the defaults are the real thing.
    """

    # macOS attributes both to the app that owns the process: the terminal
    # hosting the worker (cmux, or the terminal you started from), and the
    # one running conduct.sh for the conductor's own probe.
    # A fresh Screen Recording grant is seen live; Accessibility is
    # answered per-process by AXIsProcessTrusted and stays stale for a
    # running conductor, so that grant needs a restart to count.
    remedy = ("grant Accessibility and Screen Recording in System "
              "Settings > Privacy & Security to the terminal app that "
              "runs the workers and the conductor (heygent, when it was "
              "opened as the app; if it is not in a list, click + and "
              "choose it from Applications); a fresh Accessibility "
              "grant counts only after conduct.sh is restarted")

    def __init__(self, quartz=None, run=subprocess.run, ax=_ax_trusted,
                 pause: float = 0.02, apps=None, clipboard=None,
                 secure_input=_secure_input_on, sleep=time.sleep,
                 clock=time.monotonic, perform_press=None) -> None:
        self.quartz = quartz or _load_quartz()
        self.run = run
        self.ax = ax
        self.pause = pause
        self._apps = apps
        self._clipboard = clipboard
        self.secure_input = secure_input
        self.sleep = sleep
        self.clock = clock
        self.perform_press = perform_press or _ax_press

    @property
    def apps(self) -> MacApps:
        if self._apps is None:
            self._apps = MacApps(quartz=self.quartz)
        return self._apps

    @property
    def clipboard(self) -> MacClipboard:
        if self._clipboard is None:
            self._clipboard = MacClipboard()
        return self._clipboard

    # -- permissions --------------------------------------------------------
    def permissions(self) -> dict[str, bool]:
        preflight = getattr(self.quartz, "CGPreflightScreenCaptureAccess",
                            None)
        return {"accessibility": bool(self.ax()),
                "screen_recording": bool(preflight()) if preflight else False}

    def _require_input(self) -> None:
        if not self.permissions()["accessibility"]:
            raise ComputerError(
                "Accessibility is not granted: macOS drops posted input. "
                "Grant it in System Settings > Privacy & Security > "
                "Accessibility, then retry.")

    def keyboard_warning(self) -> str:
        """Why keystrokes posted right now may be thrown away, or "".

        Said, not refused: the keys are still posted, and the worker is
        told to look. Asked of typing, pasting and chords, never of a
        click: secure input discards posted keystrokes and lets posted
        clicks through.
        """
        if not self.secure_input():
            return ""
        try:
            front = self.apps.front()
        except Exception:
            application_log("ui", "computer.front_app_unreadable",
                            "could not name the front app for a warning",
                            severity="warning", exc_info=True)
            front = None
        # ioreg's kCGSSessionSecureInputPID looks like the holder and is
        # not: with a background python holding secure input it named the
        # front app instead (2026-09-10). So the front app is offered as
        # the likeliest place, and no pid is claimed.
        hint = (f" The front app is {front.name}, the likeliest place to "
                "look." if front is not None and front.name else "")
        return ("macOS secure input is on, so these keystrokes were "
                "probably thrown away without an error (clicks still land). "
                "A password field, a terminal with Secure Keyboard Entry or "
                "a call window turns it on, and macOS does not reliably say "
                "which." + hint + " Look to check whether the text arrived, "
                "and if secure input is the user's, tell them.")

    # -- seeing -------------------------------------------------------------
    def screenshot(self, path: str | None = None) -> Path:
        target = Path(path) if path else \
            SCREENSHOT_DIR / f"shot-{time.strftime('%H%M%S')}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        result = self.run(["screencapture", "-x", str(target)],
                          capture_output=True)
        if result.returncode != 0:
            raise ComputerError(
                "screencapture failed: "
                + (result.stderr or b"").decode(errors="replace").strip())
        application_log("ui", "computer.screenshot_taken",
                        f"screen captured to {target}",
                        data={"path": str(target)})
        return target

    # -- acting -------------------------------------------------------------
    def _post(self, event) -> None:
        self.quartz.CGEventPost(self.quartz.kCGHIDEventTap, event)
        time.sleep(self.pause)

    def move(self, x: float, y: float) -> None:
        self._require_input()
        q = self.quartz
        self._post(q.CGEventCreateMouseEvent(
            None, q.kCGEventMouseMoved, (float(x), float(y)),
            q.kCGMouseButtonLeft))
        application_log("ui", "computer.moved", f"cursor to {x},{y}",
                        data={"x": x, "y": y})

    def click(self, x: float, y: float, button: str = "left",
              count: int = 1) -> None:
        self._require_input()
        q = self.quartz
        point = (float(x), float(y))
        if button == "left":
            down, up, btn = (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp,
                             q.kCGMouseButtonLeft)
        elif button == "right":
            down, up, btn = (q.kCGEventRightMouseDown,
                             q.kCGEventRightMouseUp, q.kCGMouseButtonRight)
        else:
            raise ComputerError(f"unknown mouse button: {button!r}")
        self._post(q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved,
                                             point, btn))
        for press in range(1, count + 1):
            for kind in (down, up):
                event = q.CGEventCreateMouseEvent(None, kind, point, btn)
                q.CGEventSetIntegerValueField(
                    event, q.kCGMouseEventClickState, press)
                self._post(event)
        application_log("ui", "computer.clicked",
                        f"{button} click at {x},{y}",
                        data={"x": x, "y": y, "button": button,
                              "count": count})

    def type_text(self, text: str) -> None:
        self._require_input()
        q = self.quartz
        for start in range(0, len(text), 20):
            chunk = text[start:start + 20]
            for keydown in (True, False):
                event = q.CGEventCreateKeyboardEvent(None, 0, keydown)
                q.CGEventKeyboardSetUnicodeString(event, len(chunk), chunk)
                self._post(event)
        application_log("ui", "computer.typed",
                        f"typed {len(text)} characters",
                        data={"length": len(text)})

    def press(self, combo: str) -> None:
        self._require_input()
        q = self.quartz
        parts = [part.strip().lower() for part in combo.split("+") if
                 part.strip()]
        if not parts:
            raise ComputerError("key needs a combo, e.g. cmd+s")
        *modifiers, key = parts
        code = KEYCODES.get(key)
        if code is None:
            raise ComputerError(
                f"unknown key {key!r}; known keys: letters, digits, "
                "return, tab, space, delete, esc, arrows, home, end")
        flags = 0
        for name in modifiers:
            mask = MODIFIERS.get(name)
            if mask is None:
                raise ComputerError(f"unknown modifier {name!r}; use "
                                    "cmd, shift, alt or ctrl")
            flags |= getattr(q, mask)
        for keydown in (True, False):
            event = q.CGEventCreateKeyboardEvent(None, code, keydown)
            if flags:
                q.CGEventSetFlags(event, flags)
            self._post(event)
        application_log("ui", "computer.key_pressed", f"pressed {combo}",
                        data={"combo": combo})

    def press_element(self, element) -> bool:
        """The click, through accessibility: the user's cursor and focus
        stay where they were. False when the element refuses the press,
        which is the caller's cue to click for real."""
        self._require_input()
        if self.perform_press(element.ref) != 0:
            return False
        self.sleep(self.pause)
        application_log("ui", "computer.pressed",
                        f"pressed {element.describe()}",
                        data={"role": element.role, "app": element.app})
        return True

    def paste_text(self, text: str) -> tuple[bool, bool]:
        """Paste text into the focused field, then put the clipboard back.

        One cmd+v rather than an event per twenty characters. The
        clipboard is the user's: all of it is set aside first and
        restored after - unless something else wrote to it in between,
        the user copying something, which is then left alone rather than
        overwritten with what they had before. Returns whether it went
        back, and whether all of it could be read to go back.
        """
        if not text:
            raise ComputerError("paste needs some text")
        self._require_input()
        board = self.clipboard
        saved, whole = board.snapshot()
        ours = board.put_text(text)
        restored = False
        try:
            self.press("cmd+v")
        finally:
            self.sleep(PASTE_SETTLE_S)
            if board.change_count() == ours:
                board.restore(saved)
                restored = True
        application_log("ui", "computer.pasted",
                        f"pasted {len(text)} characters",
                        data={"length": len(text),
                              "clipboard_restored": restored,
                              "clipboard_whole": whole})
        return restored, whole

    # -- apps ---------------------------------------------------------------
    def running_apps(self) -> list[str]:
        """What `apps` prints: the app keystrokes go to, then every
        running app and whether it has a window on screen."""
        front = self.apps.front()
        counts = self.apps.windows()
        first = front.pid if front is not None else None
        lines = [f"front: {self._named(front)} - keystrokes go to this app"
                 if front is not None else "front: no app"]
        for app in sorted(self.apps.running(),
                          key=lambda a: (a.pid != first, a.name.lower())):
            lines.append(f"  {self._named(app)} - "
                         f"{self._on_screen(counts.get(app.pid, 0))}")
        return lines

    def open_app(self, name: str, front: bool = False,
                 wait: float = OPEN_WAIT_S) -> str:
        """Open or reopen an app by name, behind the user's window unless
        front. Returns what is now true, in a sentence.

        By name, never by its Dock icon: the icons sit side by side, and
        on 2026-09-09 a click one icon over turned on FaceTime's camera
        instead of opening Messages. The name becomes a bundle id first
        and everything after is keyed by that, because a name is not a
        safe key - Messages is com.apple.MobileSMS.

        `open -g` also reopens an app that is running with no window, the
        way a Dock click does, so an app like Messages that usually sits
        windowless gets one.
        """
        name = name.strip()
        if not name:
            raise ComputerError("open needs an app name, e.g. open Messages")
        bundle = self._bundle(name)
        before = self.apps.front()
        result = self.run(["open"] + ([] if front else ["-g"])
                          + ["-b", bundle], capture_output=True)
        if result.returncode != 0:
            raise ComputerError(
                f"could not open {name} ({bundle}): "
                + (result.stderr or b"").decode(errors="replace").strip())
        app, count = self._await_window(bundle, wait)
        if app is None:
            raise ComputerError(f"{name} ({bundle}) is not running {wait:g}s "
                                "after it was opened")
        restored = None
        if not front and before is not None and before.pid != app.pid:
            now = self.apps.front()
            if now is not None and now.pid == app.pid:
                # Asked for the background and came forward anyway, taking
                # the user's window: hand it back.
                restored = self._give_front_back(before)
        windows = ("with a window on screen" if count else
                   "but no window on screen - it may be minimised, hidden "
                   "or on another Space")
        now = self.apps.front()
        if front:
            said = (f"{app.name} is open in front {windows}."
                    if now is not None and now.pid == app.pid else
                    f"{app.name} is open {windows}, but "
                    f"{now.name if now is not None else 'no app'} is in "
                    "front.")
        elif restored is True:
            said = (f"{app.name} is open {windows}. It came to the front "
                    f"anyway, so {before.name} was put back in front.")
        elif restored is False:
            said = (f"{app.name} is open {windows}. It came to the front "
                    f"anyway and {before.name} could not be put back - "
                    "look before acting.")
        else:
            said = (f"{app.name} is open in the background {windows}; "
                    f"{now.name if now is not None else 'no app'} is in "
                    "front.")
        application_log("ui", "computer.app_opened", said,
                        data={"app": app.name, "bundle": bundle,
                              "front": front, "windows": count,
                              "restored": restored})
        return said

    def _bundle(self, name: str) -> str:
        """The bundle id a name means: a running app showing exactly that
        name first, then whatever Launch Services resolves it to. Nothing
        is guessed from a partial name."""
        wanted = name.lower().removesuffix(".app")
        running = self.apps.running()
        for app in running:
            if app.name.lower() == wanted and app.bundle:
                return app.bundle
        bundle = self.apps.bundle_for(name)
        if bundle:
            return bundle
        names = ", ".join(sorted({app.name for app in running if app.name},
                                 key=str.lower))
        raise ComputerError(f"no app called {name!r} was found. Use the "
                            "name the app shows in its menu bar; running "
                            f"now: {names}")

    def _await_window(self, bundle: str, wait: float) -> tuple[App | None, int]:
        """The app once it is running and has a window, or whatever there
        is when the wait runs out."""
        deadline = self.clock() + wait
        while True:
            app = next((a for a in self.apps.running() if a.bundle == bundle),
                       None)
            count = self.apps.windows().get(app.pid, 0) if app else 0
            if (app is not None and count) or self.clock() >= deadline:
                return app, count
            self.sleep(0.1)

    def _give_front_back(self, app: App) -> bool:
        """Activate the app that was in front, and say whether it is."""
        self.apps.activate(app.pid)
        deadline = self.clock() + 1.0
        while True:
            now = self.apps.front()
            if now is not None and now.pid == app.pid:
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(0.1)

    @staticmethod
    def _named(app: App) -> str:
        return f"{app.name} ({app.bundle})" if app.bundle else app.name

    @staticmethod
    def _on_screen(count: int) -> str:
        # Whether, not how many: what the window list calls an app's
        # windows includes pieces nobody would. Full-screen Chrome listed
        # its 1728x962 page beside 1728x41, 1728x81 and 1728x158 toolbar
        # strips (2026-09-10), and "4 windows" was the answer.
        return "window on screen" if count else "no window on screen"


# X11 keysym spellings for the key names that are not already keysyms.
X11_KEYSYMS = {
    "return": "Return", "enter": "Return", "tab": "Tab", "space": "space",
    "delete": "BackSpace", "backspace": "BackSpace", "escape": "Escape",
    "esc": "Escape", "left": "Left", "right": "Right", "up": "Up",
    "down": "Down", "home": "Home", "end": "End", "pageup": "Prior",
    "pagedown": "Next", "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
    "f5": "F5", "f6": "F6",
}

# What each chord modifier means off-macOS: cmd is the shortcut key, and
# the shortcut key elsewhere is ctrl (cmd+s means save everywhere).
PORTABLE_MODIFIERS = {"cmd": "ctrl", "command": "ctrl", "ctrl": "ctrl",
                      "control": "ctrl", "shift": "shift", "alt": "alt",
                      "option": "alt"}


class X11Driver:
    """xdotool/scrot control of an X11 session (Linux)."""

    remedy = ("start an X11 session (DISPLAY) and install xdotool "
              "and scrot")

    def __init__(self, run=subprocess.run, env=None,
                 which=shutil.which) -> None:
        self.run = run
        self.env = dict(env if env is not None else os.environ)
        self.which = which

    def permissions(self) -> dict[str, bool]:
        return {"display": bool(self.env.get("DISPLAY")),
                "input tool (xdotool)": self.which("xdotool") is not None,
                "screenshot tool (scrot)": self.which("scrot") is not None}

    def _require_input(self) -> None:
        missing = [name for name, ok in self.permissions().items()
                   if not ok and "scrot" not in name]
        if missing:
            raise ComputerError("cannot post input: missing "
                                + " and ".join(missing))

    def _xdo(self, *args: str) -> None:
        result = self.run(["xdotool", *args], capture_output=True,
                          env=self.env)
        if result.returncode != 0:
            raise ComputerError(
                "xdotool failed: "
                + (result.stderr or b"").decode(errors="replace").strip())

    def screenshot(self, path: str | None = None) -> Path:
        if self.which("scrot") is None or not self.env.get("DISPLAY"):
            raise ComputerError(f"cannot capture the screen: {self.remedy}")
        target = Path(path) if path else \
            SCREENSHOT_DIR / f"shot-{time.strftime('%H%M%S')}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        result = self.run(["scrot", "-o", str(target)],
                          capture_output=True, env=self.env)
        if result.returncode != 0:
            raise ComputerError(
                "scrot failed: "
                + (result.stderr or b"").decode(errors="replace").strip())
        application_log("ui", "computer.screenshot_taken",
                        f"screen captured to {target}",
                        data={"path": str(target)})
        return target

    def move(self, x: float, y: float) -> None:
        self._require_input()
        self._xdo("mousemove", str(int(x)), str(int(y)))
        application_log("ui", "computer.moved", f"cursor to {x},{y}",
                        data={"x": x, "y": y})

    def click(self, x: float, y: float, button: str = "left",
              count: int = 1) -> None:
        self._require_input()
        if button == "left":
            number = "1"
        elif button == "right":
            number = "3"
        else:
            raise ComputerError(f"unknown mouse button: {button!r}")
        self._xdo("mousemove", str(int(x)), str(int(y)))
        self._xdo("click", "--repeat", str(count), number)
        application_log("ui", "computer.clicked",
                        f"{button} click at {x},{y}",
                        data={"x": x, "y": y, "button": button,
                              "count": count})

    def type_text(self, text: str) -> None:
        self._require_input()
        self._xdo("type", "--delay", "25", "--", text)
        application_log("ui", "computer.typed",
                        f"typed {len(text)} characters",
                        data={"length": len(text)})

    def press(self, combo: str) -> None:
        self._require_input()
        parts = [part.strip().lower() for part in combo.split("+")
                 if part.strip()]
        if not parts:
            raise ComputerError("key needs a combo, e.g. cmd+s")
        *modifiers, key = parts
        if key not in KEYCODES:
            raise ComputerError(
                f"unknown key {key!r}; known keys: letters, digits, "
                "return, tab, space, delete, esc, arrows, home, end")
        mapped = []
        for name in modifiers:
            portable = PORTABLE_MODIFIERS.get(name)
            if portable is None:
                raise ComputerError(f"unknown modifier {name!r}; use "
                                    "cmd, shift, alt or ctrl")
            mapped.append(portable)
        self._xdo("key", "+".join(mapped + [X11_KEYSYMS.get(key, key)]))
        application_log("ui", "computer.key_pressed", f"pressed {combo}",
                        data={"combo": combo})


# Windows virtual-key codes for chords; `type` goes through the unicode
# path of SendInput and needs no table.
WINDOWS_VKEYS = {
    "return": 0x0D, "enter": 0x0D, "tab": 0x09, "space": 0x20,
    "delete": 0x08, "backspace": 0x08, "escape": 0x1B, "esc": 0x1B,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75,
}
WINDOWS_MODIFIER_VKEYS = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12}
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_MOUSE_FLAGS = {("left", True): 0x0002, ("left", False): 0x0004,
                ("right", True): 0x0008, ("right", False): 0x0010}


def _input_struct():
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("ki", KEYBDINPUT),
                    ("padding", ctypes.c_ubyte * 8)]

    return INPUT


_INPUT_TYPE_KEYBOARD = 1
_INPUT_CLASS = None
_INPUT_SIZE = 0


def _keyboard_input(scan: int, flags: int):
    """One INPUT struct for SendInput, unicode character in wScan."""
    import ctypes
    global _INPUT_CLASS, _INPUT_SIZE
    if _INPUT_CLASS is None:
        _INPUT_CLASS = _input_struct()
        _INPUT_SIZE = ctypes.sizeof(_INPUT_CLASS)
    struct = _INPUT_CLASS()
    struct.type = _INPUT_TYPE_KEYBOARD
    struct.ki.wVk = 0
    struct.ki.wScan = scan
    struct.ki.dwFlags = flags
    return ctypes.byref(struct)


_WINDOWS_CAPTURE = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
    "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
    "$img = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
    "$g = [System.Drawing.Graphics]::FromImage($img); "
    "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $img.Size); "
    "$img.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png)")


def _load_user32():
    import ctypes
    try:
        return ctypes.windll.user32
    except AttributeError as exc:
        raise ComputerError("computer use needs Windows user32") from exc


class WindowsDriver:
    """user32-level control of the screen, cursor and keyboard (Windows).

    Input goes through keybd_event/mouse_event and unicode SendInput;
    the screen is captured with a PowerShell one-liner. user32 and run
    are injectable so the call grammar is testable off-Windows.
    """

    remedy = "needs a Windows desktop session"

    def __init__(self, user32=None, run=subprocess.run,
                 pause: float = 0.02) -> None:
        self.user32 = user32 or _load_user32()
        self.run = run
        self.pause = pause

    def permissions(self) -> dict[str, bool]:
        return {"desktop input": True, "screen capture": True}

    def screenshot(self, path: str | None = None) -> Path:
        target = Path(path) if path else \
            SCREENSHOT_DIR / f"shot-{time.strftime('%H%M%S')}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        script = _WINDOWS_CAPTURE.format(
            path=str(target).replace("'", "''"))
        result = self.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True)
        if result.returncode != 0:
            raise ComputerError(
                "screen capture failed: "
                + (result.stderr or b"").decode(errors="replace").strip())
        application_log("ui", "computer.screenshot_taken",
                        f"screen captured to {target}",
                        data={"path": str(target)})
        return target

    def _pause(self) -> None:
        time.sleep(self.pause)

    def move(self, x: float, y: float) -> None:
        self.user32.SetCursorPos(int(x), int(y))
        self._pause()
        application_log("ui", "computer.moved", f"cursor to {x},{y}",
                        data={"x": x, "y": y})

    def click(self, x: float, y: float, button: str = "left",
              count: int = 1) -> None:
        if button not in ("left", "right"):
            raise ComputerError(f"unknown mouse button: {button!r}")
        self.user32.SetCursorPos(int(x), int(y))
        self._pause()
        for _ in range(count):
            for down in (True, False):
                self.user32.mouse_event(
                    _MOUSE_FLAGS[(button, down)], 0, 0, 0, 0)
                self._pause()
        application_log("ui", "computer.clicked",
                        f"{button} click at {x},{y}",
                        data={"x": x, "y": y, "button": button,
                              "count": count})

    def type_text(self, text: str) -> None:
        for char in text:
            for flags in (_KEYEVENTF_UNICODE,
                          _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
                sent = self.user32.SendInput(
                    1, _keyboard_input(ord(char), flags), _INPUT_SIZE)
                if sent != 1:
                    raise ComputerError("Windows rejected the keystroke "
                                        "(SendInput sent nothing)")
                self._pause()
        application_log("ui", "computer.typed",
                        f"typed {len(text)} characters",
                        data={"length": len(text)})

    def press(self, combo: str) -> None:
        parts = [part.strip().lower() for part in combo.split("+")
                 if part.strip()]
        if not parts:
            raise ComputerError("key needs a combo, e.g. cmd+s")
        *modifiers, key = parts
        if len(key) == 1 and key.isalnum():
            code = ord(key.upper())
        elif key in WINDOWS_VKEYS:
            code = WINDOWS_VKEYS[key]
        else:
            raise ComputerError(
                f"unknown key {key!r}; known keys: letters, digits, "
                "return, tab, space, delete, esc, arrows, home, end")
        vkeys = []
        for name in modifiers:
            portable = PORTABLE_MODIFIERS.get(name)
            if portable is None:
                raise ComputerError(f"unknown modifier {name!r}; use "
                                    "cmd, shift, alt or ctrl")
            vkeys.append(WINDOWS_MODIFIER_VKEYS[portable])
        for vkey in vkeys + [code]:
            self.user32.keybd_event(vkey, 0, 0, 0)
            self._pause()
        for vkey in [code] + list(reversed(vkeys)):
            self.user32.keybd_event(vkey, 0, _KEYEVENTF_KEYUP, 0)
            self._pause()
        application_log("ui", "computer.key_pressed", f"pressed {combo}",
                        data={"combo": combo})


def make_driver(platform: str | None = None):
    """The backend for this machine, picked by platform."""
    platform = platform or sys.platform
    if platform == "darwin":
        return Driver()
    if platform.startswith("linux"):
        return X11Driver()
    if platform in ("win32", "cygwin"):
        return WindowsDriver()
    raise ComputerError(f"computer use is not supported on {platform}")


def make_guard(platform: str | None = None, eyes=None,
               directory: str | Path | None = None) -> "Guard | None":
    """The screen guard for this machine, or None where there is none.

    Only macOS has the window, focus and event-source APIs the guard is
    built out of - and macOS is where the damage happened. Elsewhere the
    lease still holds; the target check does not exist and is not
    pretended into existence.
    """
    platform = platform or sys.platform
    if platform != "darwin":
        return None
    return Guard(directory or conductor_home(), eyes=eyes or MacEyes(),
                 task_id=os.environ.get(instance.TASK_ENV, ""))


# What each verb aims at, and therefore what has to still be true before
# it may happen. See Guard.check.
TARGETS = {"click": "point", "type": "focus", "key": "focus",
           "paste": "focus", "move": "none"}

# How long to let the screen catch up with what we just did before
# recording it as the new baseline. A window takes a moment to come
# forward and the event counters a moment to count.
SETTLE_S = 0.25


def permit(action: str, point: tuple | None = None, expect: str = "",
           guard: "Guard | None" = None,
           lease=instance.lease,
           into: str = "") -> "tuple[Element | None, list[str]]":
    """What the worker should be told about an action it is taking.

    Nothing here stops the action. The user wants a worker that is told
    rather than stopped (2026-09-10), so every answer is a warning printed
    after the action, for the worker to read, look, and put right.

    First: is this worker still held by a live conductor? A worker whose
    conductor died, or that a restart did not re-adopt, is a hand on the
    keyboard nobody owns - on 2026-09-08 one of those was still driving
    Chrome hours after the conductor that started it had gone.

    Then: is the screen still what we last looked at? See screen_guard.

    Last: what is it aimed at, and does that look wrong - typing or
    pasting with no text field focused, a click on the Dock?

    Returns the target when macOS can say, so the action can name it and
    a click can press it quietly, and the warnings.
    """
    warnings = []
    held = lease(None, home=conductor_home())
    if not held:
        warnings.append(held.reason)
    if guard is None:
        return None, warnings
    warnings += guard.check(action, point=point, expect=expect,
                            target=TARGETS.get(action, "front"))
    target, aimed = None, ""
    if action in ("type", "paste"):
        target, aimed = guard.typing_target(expect=expect, into=into)
    elif action == "click" and point is not None:
        target, aimed = guard.click_target(*point, expect=expect)
    return target, warnings + ([aimed] if aimed else [])


def say_warnings(notes: list[str], command: str, out) -> None:
    """Said, not stopped: the action has happened, and the worker reads
    what may have gone wrong with it, one `warning:` line each."""
    for note in notes:
        out(f"warning: {note}")
        application_log("ui", "computer.action_warned", note[:400],
                        severity="warning", data={"command": command})


def cli_command() -> str:
    """The exact command a worker runs, wherever its cwd is.

    On uv's managed runtime, the same one conduct.sh and boss-mcp run
    on: this file declares its own dependencies, so uv brings the Quartz
    bindings and a worker's `python3` - Homebrew's, Xcode's, whatever the
    shell finds - never has to carry them. Without uv the plain
    interpreter is all there is, and `check` will say what is missing.
    """
    from conductor.boss_helper import PYTHON_PIN, find_uv
    script = Path(__file__).resolve()
    uv = find_uv()
    if not uv:
        return f"python3 {script}"
    return (f'"{uv}" run --python-preference only-managed '
            f"--python {PYTHON_PIN} --no-project --quiet --script {script}")


def worker_brief() -> str:
    """The computer-use section of a worker's first message: only tasks the
    user opened with computer use ever see it."""
    cli = cli_command()
    return (
        "The user has allowed this task to operate this machine's GUI. "
        "Drive it with these commands (through Bash):\n"
        f"  {cli} look [PATH.png]  - screenshot the screen, then Read it\n"
        f"  {cli} apps            - running apps, the one keystrokes go "
        "to, and which have a window\n"
        f"  {cli} open APP [--front] - open or reopen an app by name, "
        "behind the user's window unless --front\n"
        f"  {cli} click X Y [--right] [--double] [--pointer] [--expect APP]\n"
        f"  {cli} move X Y\n"
        f"  {cli} type TEXT [--expect APP] [--into FIELD]\n"
        f"  {cli} paste TEXT [--expect APP] [--into FIELD] - put text in "
        "the focused field in one keystroke; the clipboard is put back\n"
        f"  {cli} key COMBO       - a chord, e.g. key cmd+s (cmd means "
        "ctrl off-macOS)\n"
        f"  {cli} check           - permissions, and whether you still "
        "hold the screen\n"
        "Look before and after every action: never click blind, and "
        "verify each action did what you expected before the next. "
        "Coordinates are screen points, origin at the top left. The user "
        "is on this machine right now: touch only the windows your task "
        "needs, and still never play audio or use the microphone.\n"
        "Reach an app with `open`, never by clicking its Dock icon: the "
        "icons sit side by side, and a click one over opens the wrong "
        "app. Pass --front when the user should see it, and look after "
        "opening - a background window can sit behind others. Before "
        "typing, `apps` says which app the keystrokes will reach. Prefer "
        "paste to type for more than a few words, and click into the "
        "field first. A left click on a button, link or menu item is "
        "pressed through accessibility, leaving the user's cursor alone; "
        "if that visibly did nothing, repeat it with --pointer. apps, "
        "open and paste are macOS-only.\n"
        "Nothing you do is refused. An action that looks wrong still "
        "happens, and its output ends with a `warning:` line saying why: "
        "read it, look, and put right what went wrong - and never report "
        "text as typed when a warning says it may not have landed. You "
        "get one when the screen changed since your last look (a key or "
        "click that was not yours, a new front window, a moved caret - "
        "the user is on this machine and other agents may be too, so if "
        "the screen is someone else's right now, say so rather than "
        "typing over them), when a click hit the Dock, when typing or "
        "pasting had no text field or was outside the app and field that "
        "--expect and --into (e.g. --into search) name, when macOS secure "
        "input was on and the keystrokes were probably discarded, and "
        "when the conductor that gave you this task is gone - then tell "
        "the user. Pass --expect with the app name whenever you know it.")


# Verbs only the macOS driver has: they ask NSWorkspace and the pasteboard,
# which the X11 and Windows backends have no counterpart for yet.
MAC_ONLY = ("apps", "open", "paste")


def _mac_only(driver, command: str) -> None:
    if not isinstance(driver, Driver):
        raise ComputerError(f"`{command}` is macOS-only for now; on this "
                            "platform use look, click, type and key")


def main(argv: list[str] | None = None,
         driver_factory=make_driver, out=print,
         guard_factory=make_guard) -> int:
    parser = argparse.ArgumentParser(
        prog="computer", description="Drive this machine's GUI: see the "
                                     "screen, click, type.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="report whether this platform's GUI "
                                 "permissions and tools are in place")
    look = sub.add_parser("look", help="screenshot the screen to a png")
    look.add_argument("path", nargs="?", default=None)
    click = sub.add_parser("click", help="click at screen coordinates")
    click.add_argument("x", type=float)
    click.add_argument("y", type=float)
    click.add_argument("--right", action="store_true")
    click.add_argument("--double", action="store_true")
    click.add_argument("--pointer", action="store_true",
                       help="a real pointer click, even on a control that "
                            "could be pressed without moving the cursor")
    move = sub.add_parser("move", help="move the cursor")
    move.add_argument("x", type=float)
    move.add_argument("y", type=float)
    type_ = sub.add_parser("type", help="type literal text")
    type_.add_argument("text")
    key = sub.add_parser("key", help="press a key chord, e.g. cmd+s")
    key.add_argument("combo")
    sub.add_parser("apps", help="list running apps, the one keystrokes go "
                                "to, and which have a window (macOS)")
    open_ = sub.add_parser("open", help="open or reopen an app by name, in "
                                        "the background unless --front "
                                        "(macOS)")
    open_.add_argument("app")
    open_.add_argument("--front", action="store_true",
                       help="bring it to the front")
    paste = sub.add_parser("paste", help="paste text into the focused field "
                                         "and put the clipboard back "
                                         "(macOS)")
    paste.add_argument("text")
    for landing in (type_, paste):
        landing.add_argument("--into", default="", metavar="FIELD",
                             help="the field this is for, e.g. search; warn "
                                  "if the caret is somewhere else")
    # Say which app this was meant for and the guard checks it, rather
    # than only checking that nothing changed. Cheap, and it is the one
    # thing the driver cannot work out for itself.
    for acting in (click, move, type_, key, paste):
        acting.add_argument("--expect", default="", metavar="APP",
                            help="the app this action is for; warn if "
                                 "the front window is not it")
    args = parser.parse_args(argv)
    try:
        driver = driver_factory()
        guard = guard_factory()
        if args.command in MAC_ONLY:
            _mac_only(driver, args.command)
        if args.command == "check":
            permissions = driver.permissions()
            for name, granted in permissions.items():
                out(f"{name}: {'granted' if granted else 'NOT granted'}")
            held = instance.lease(None, home=conductor_home())
            out(f"gui lease: {'held' if held else 'NOT held'} - {held.reason}")
            return 0 if all(permissions.values()) and held else 1
        if args.command == "look":
            path = driver.screenshot(args.path)
            # A look is also the baseline every later action is measured
            # against: what the screen was when this picture was taken.
            if guard is not None:
                guard.record(str(path))
            out(str(path))
            return 0
        if args.command == "apps":
            for line in driver.running_apps():
                out(line)
            return 0
        if args.command == "open":
            # The lease, and not the guard. Opening aims at no point and no
            # caret, so there is nothing for a look to verify, and demanding
            # one would put a screenshot in front of the one action that
            # needs none. Whatever it changes on screen is caught by the
            # guard on the next click or keystroke.
            _, notes = permit("open")
            out(driver.open_app(args.app, front=args.front))
            say_warnings(notes, args.command, out)
            return 0
        point = (args.x, args.y) if args.command in ("click", "move") else None
        target, notes = permit(args.command, point=point, expect=args.expect,
                               guard=guard, into=getattr(args, "into", ""))
        if args.command in ("type", "key", "paste"):
            discarded = getattr(driver, "keyboard_warning", lambda: "")()
            if discarded:
                notes.append(discarded)
        landed = (f" into {target.describe()}"
                  if target is not None and target.pid else "")
        if args.command == "click":
            quietly = (target is not None and target.pressable
                       and not target.text_entry and not args.pointer
                       and not args.right and not args.double)
            if quietly and driver.press_element(target):
                out(f"pressed {target.describe()} at {args.x:g},{args.y:g} "
                    "without moving the cursor")
            else:
                driver.click(args.x, args.y,
                             button="right" if args.right else "left",
                             count=2 if args.double else 1)
                out(f"clicked {args.x:g},{args.y:g}"
                    + (f" on {target.describe()}" if target else ""))
        elif args.command == "move":
            driver.move(args.x, args.y)
            out(f"moved to {args.x:g},{args.y:g}")
        elif args.command == "type":
            driver.type_text(args.text)
            out(f"typed {len(args.text)} characters{landed}")
        elif args.command == "key":
            driver.press(args.combo)
            out(f"pressed {args.combo}")
        elif args.command == "paste":
            restored, whole = driver.paste_text(args.text)
            if not restored:
                board = ("something else wrote to the clipboard meanwhile, "
                         "so it was left as it is")
            elif whole:
                board = "the clipboard is back as it was"
            else:
                board = ("the clipboard is back, except content an app had "
                         "only promised and never wrote")
            out(f"pasted {len(args.text)} characters{landed}; {board}")
        say_warnings(notes, args.command, out)
        # What our own action left behind becomes the new baseline, so the
        # next check asks "what has changed since WE acted" rather than
        # flagging our own key press as somebody else's. After a settle:
        # the window list and the event counters both take a moment to
        # show what we just did, and a baseline taken too early puts our
        # own click on somebody else's account.
        if guard is not None:
            time.sleep(SETTLE_S)
            guard.record()
    except ComputerError as exc:
        out(str(exc))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
