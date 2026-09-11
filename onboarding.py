#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "numpy>=1.26,<3",
#   "sounddevice>=0.4.6,<1",
#   "pyobjc-framework-Cocoa>=10,<13",
#   "pyobjc-framework-Quartz>=10,<13",
#   "pyobjc-framework-CoreText>=10,<13",
#   "pyobjc-framework-AVFoundation>=10,<13",
#   "pyobjc-framework-Speech>=10,<13",
# ]
# ///

"""First-run onboarding, in the spirit of Wispr Flow's.

One question per screen, and every permission step is verified rather than
taken on trust: the flow watches for the grant to actually appear and
advances itself, the same way Wispr Flow's onboarding does. The only
yes/no questions are the two self-checks that need a human - "do you see
purple bars while you speak?" and "does the key light up while you hold
it?" - because no probe can answer those.

    ./conduct.sh --onboard

Steps, in order:

    welcome           what this is, one Continue button
    input_monitoring  the Fn hotkey needs an event tap; opens the pane,
                      advances when the grant appears
    microphone        opens the input stream, which is what makes macOS
                      ask; advances when audio is flowing
    accessibility     computer use clicks and types; asks macOS outright
                      and opens the pane, advances when the grant appears
    screen_recording  computer use reads the screen; same shape as above
    mic_test          live purple bars driven by the microphone
    choose_key        pick the push-to-talk key; saved for hotkey.py
    hotkey_test       the chosen key's graphic lights while it is held
    done              what to do once the conductor starts

The step machine is pure and lives in OnboardingFlow, so the order and the
auto-advance rules are tested without AppKit or a Mac (the FnGate pattern,
see hotkey.py). The AppKit shell below is rendering and probes only.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from hotkey import (CONFIG_PATH, KEY_GLYPHS, KEY_LABELS, KEY_MASKS,
                    chosen_key, key_held, save_key)

# The panes each permission lives in, opened for the user rather than
# described to them (gui_permissions.py does the same for computer use).
PANES = {
    "input_monitoring": ("x-apple.systempreferences:com.apple.preference."
                         "security?Privacy_ListenEvent"),
    "microphone": ("x-apple.systempreferences:com.apple.preference."
                   "security?Privacy_Microphone"),
    "accessibility": ("x-apple.systempreferences:com.apple.preference."
                      "security?Privacy_Accessibility"),
    "screen_recording": ("x-apple.systempreferences:com.apple.preference."
                         "security?Privacy_ScreenCapture"),
}

STEPS = ("welcome", "input_monitoring", "microphone",
         "accessibility", "screen_recording",
         "mic_test", "choose_key", "hotkey_test", "done")

# The accounts the conductor runs on: the OpenAI key the voice uses and
# the Anthropic sign-in Claude Code uses. The CLI flow carries them (the
# terminal is where a browser hand-off and a hidden paste are natural);
# both are skipped outright on a machine already signed in.
ACCOUNT_STEPS = ("openai_key", "claude_login")

# cmux hosts the workers. Installing it here, in front of the user, is
# what moves the Homebrew wait and Gatekeeper's "downloaded from the
# internet" question out of the conductor's first start; a machine that
# already has cmux skips the step outright. Terminal-only, like the
# account steps: the GUI flow leaves it to the conductor.
CMUX_STEPS = ("install_cmux",)
CLI_STEPS = STEPS[:-1] + CMUX_STEPS + ACCOUNT_STEPS + ("done",)

# The step rail across the top, one segment per stage; both front ends
# render it (the GUI as the nav bar, the CLI as a breadcrumb line).
STAGES = (("WELCOME", ("welcome",)),
          ("PERMISSIONS", ("input_monitoring", "microphone",
                           "accessibility", "screen_recording")),
          ("MIC CHECK", ("mic_test",)),
          ("SET UP", ("choose_key", "hotkey_test")),
          ("DONE", ("done",)))

CLI_STAGES = (STAGES[:-2]
              + ((STAGES[-2][0], STAGES[-2][1] + CMUX_STEPS),
                 ("ACCOUNTS", ACCOUNT_STEPS), STAGES[-1]))

# What each screen says. Kept as data so the copy is testable and the
# renderer stays dumb.
COPY = {
    "welcome": (
        "Hold a key, talk to your agents",
        "This is push-to-talk for coding agents: hold your talk key, "
        "say what you want done, and let go. A conductor starts and "
        "manages Claude Code agents on your words - the microphone is "
        "off and nothing is typed until the key is down."),
    "input_monitoring": (
        "Allow Input Monitoring",
        "macOS only reports key presses to apps you approve, and the "
        "conductor needs to notice your talk key even while another "
        "app is focused. Turn it on for this terminal in the pane that "
        "just opened - this screen moves on by itself."),
    "microphone": (
        "Allow the microphone",
        "This is how your words reach the agents. It records only "
        "while the talk key is held. Allow access when macOS asks - "
        "this screen moves on by itself."),
    "accessibility": (
        "Allow Accessibility",
        "Computer-use tasks click and type on your behalf, and macOS "
        "only lets apps you approve post those events. Turn it on for "
        "this terminal in the pane that just opened - this screen moves "
        "on by itself."),
    "screen_recording": (
        "Allow Screen Recording",
        "Computer-use tasks read the screen through screenshots. Turn "
        "it on for this terminal in the pane that just opened; if macOS "
        "offers to relaunch the terminal, choose Later so onboarding "
        "can finish. This screen moves on by itself."),
    "mic_test": (
        "Test your microphone",
        "Say a few words at a normal volume - the purple bars should "
        "rise and fall with your voice. If they stay flat, pick a "
        "different input device under System Settings > Sound. "
        "Do the bars move while you talk?"),
    "choose_key": (
        "Pick your talk key",
        "This is the key you hold down to talk: press it, speak, and "
        "release it to send. Pick one you don't already hold for "
        "shortcuts. It's saved to ~/.voice-conductor/hotkey.json, and "
        "rerunning onboarding changes it."),
    "hotkey_test": (
        "Hold the {key} key",
        "Does the key light up while you press it?"),
    "install_cmux": (
        "Install cmux",
        "Workers run in cmux, a terminal built for agents. Enter "
        "installs it with Homebrew and opens it once - when macOS asks "
        "about an app downloaded from the internet, choose Open. "
        "Skipped, the conductor installs it at first start instead."),
    "openai_key": (
        "Connect OpenAI",
        "The ear and the mouth run on OpenAI's GPT Live API. Paste a "
        "project key with GPT Live access from "
        "platform.openai.com/api-keys - the paste is hidden and the key "
        "is saved to .env next to the app, so there is no file to edit. "
        "A key exported in your shell overrides the saved one."),
    "claude_login": (
        "Sign in to Claude",
        "The coding agents run on Claude Code, which signs in with "
        "your Anthropic account. Signing in opens your browser; Claude "
        "Code keeps the login itself, so nothing lands in any file "
        "here."),
    "done": (
        "You're set",
        "Hold {key}, say what you want done, and let go. The conductor "
        "starts agents on it and tells you when they finish."),
}


class OnboardingFlow:
    """The step machine, free of AppKit so it can be tested anywhere.

    Permission steps advance on evidence (their probe returning True),
    never on a click; question steps advance on `confirm` and record a
    `deny` without moving, so the screen can show help and keep asking.
    """

    AUTO = ("input_monitoring", "microphone",
            "accessibility", "screen_recording")

    # Steps that are skipped when their probe already says yes but, unlike
    # AUTO, advance on `confirm`: the account and install steps, where the
    # screen runs an action (a paste, a browser sign-in, a brew install)
    # rather than waiting on a grant.
    SKIP_GRANTED = AUTO + CMUX_STEPS + ACCOUNT_STEPS

    def __init__(self, probes: dict[str, object],
                 key: str = "fn", steps: tuple[str, ...] = STEPS) -> None:
        self.probes = dict(probes)
        self.steps = tuple(steps)
        self.index = 0
        self.denied = False        # the user said no to the current check
        self.key = key             # the chosen push-to-talk key

    @property
    def step(self) -> str:
        return self.steps[self.index]

    def _advance(self) -> None:
        if self.index < len(self.steps) - 1:
            self.index += 1
            self.denied = False
        # A permission granted before its screen is reached should never
        # be asked for: skip straight over steps whose probe already says
        # yes, or the flow would sit waiting for a grant that exists.
        while self.step in self.SKIP_GRANTED and self._probe(self.step):
            self.index += 1
            self.denied = False

    def _probe(self, step: str) -> bool:
        probe = self.probes.get(step)
        return bool(probe()) if callable(probe) else False

    def poll(self) -> bool:
        """A moment passing. True when the flow moved on its own."""
        if self.step in self.AUTO and self._probe(self.step):
            self._advance()
            return True
        return False

    def confirm(self) -> None:
        """The user pressed the affirmative button on this screen."""
        if self.step not in self.AUTO and self.step != "choose_key":
            self._advance()

    def choose(self, key: str) -> None:
        """The user picked a push-to-talk key. Only the picker moves."""
        if self.step == "choose_key" and key in KEY_MASKS:
            self.key = key
            self._advance()

    def deny(self) -> None:
        """The user said no to a self-check. The flow stays put."""
        self.denied = True

    def back(self) -> None:
        """The user stepped back one screen. Permission steps are
        evidence, not screens to revisit: going back lands on the
        previous manual step."""
        i = self.index - 1
        while i > 0 and self.steps[i] in self.AUTO:
            i -= 1
        if i >= 0:
            self.index = i
            self.denied = False

    @property
    def finished(self) -> bool:
        return self.step == "done"


# ---------------------------------------------------------------------------
# Accounts: the OpenAI key in .env and the Claude Code sign-in.

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"


def saved_openai_key(path: Path = ENV_PATH) -> str:
    """The OPENAI_API_KEY in `path`, or "" (voice_agent.load_env's read)."""
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "OPENAI_API_KEY":
            return value.strip().strip('"').strip("'")
    return ""


def openai_key_saved(path: Path = ENV_PATH) -> bool:
    return bool(saved_openai_key(path))


def save_openai_key(key: str, path: Path = ENV_PATH) -> None:
    """Write the key to .env the way voice_agent.ask_for_api_key does:
    any existing OPENAI_API_KEY line is replaced, everything else kept."""
    existing = path.read_text() if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    lines = [line for line in existing.splitlines()
             if not line.strip().startswith("OPENAI_API_KEY")]
    lines.append(f"OPENAI_API_KEY={key}")
    path.write_text("\n".join(lines) + "\n")


def logged_in(status_json: str) -> bool:
    """What `claude auth status` says, read strictly: anything that does
    not parse as JSON with a true loggedIn reads as signed out."""
    try:
        status = json.loads(status_json)
    except ValueError:
        return False
    return isinstance(status, dict) and bool(status.get("loggedIn"))


def claude_logged_in() -> bool:
    """Is Claude Code signed in, as the conductor will run it? The
    launcher scrubs ANTHROPIC_API_KEY before starting anything (a stale
    key silently overrides the stored login), so the probe asks without
    it too - an env key must not read as a working sign-in."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        done = subprocess.run(["claude", "auth", "status"], env=env,
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return logged_in(done.stdout)


def request_grant(step: str) -> None:
    """Ask macOS outright, so the app appears in the pane's list instead
    of the user hunting for a + button. Best effort: old bindings simply
    leave the pane to do the talking."""
    names = {"accessibility": "CGRequestPostEventAccess",
             "screen_recording": "CGRequestScreenCaptureAccess"}
    name = names.get(step)
    if name is None:
        return
    try:
        import Quartz
        request = getattr(Quartz, name, None)
        if request is not None:
            request()
    except Exception:
        pass


def open_pane(step: str) -> bool:
    request_grant(step)
    url = PANES.get(step)
    if not url:
        return False
    try:
        done = subprocess.run(["open", url], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


# ---------------------------------------------------------------------------
# Probes. Each answers "is this granted, right now?" and never blocks.

def input_monitoring_granted() -> bool:
    import Quartz
    preflight = getattr(Quartz, "CGPreflightListenEventAccess", None)
    if preflight is not None:
        return bool(preflight())
    # Old bindings: the only way to ask is to try. Listen-only, torn down
    # at once, and never swallows a key.
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged),
        lambda proxy, kind, event, refcon: event, None)
    if tap is None:
        return False
    Quartz.CGEventTapEnable(tap, False)
    return True


class ChildPreflight:
    """A computer-use grant, probed in a fresh process each time.

    CGPreflightPostEventAccess and CGPreflightScreenCaptureAccess answer
    for the process's start-of-life state: a grant given while onboarding
    runs never shows in the process that asks for it. A child process sees
    the truth, so the probe asks one - off the calling thread, because the
    GUI polls from its 20 Hz tick - and remembers a yes for good.

    Unknowable is not missing (gui_permissions reads it the same way):
    old bindings and a failed child both read as granted, so the step
    skips rather than waits on a grant no probe can see.
    """

    def __init__(self, name: str):
        self.name = name
        self.verdict = False
        self.thread: threading.Thread | None = None

    def __call__(self) -> bool:
        import Quartz
        preflight = getattr(Quartz, self.name, None)
        if preflight is None or bool(preflight()) or self.verdict:
            return True
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._ask, daemon=True)
            self.thread.start()
        return False

    def _ask(self) -> None:
        code = (f"import sys, Quartz\n"
                f"f = getattr(Quartz, {self.name!r}, None)\n"
                f"sys.exit(0 if f is None or f() else 1)")
        try:
            done = subprocess.run([sys.executable, "-c", code],
                                  capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            self.verdict = True
            return
        if done.returncode == 0:
            self.verdict = True
        else:
            time.sleep(1.0)           # a breath between child processes


accessibility_granted = ChildPreflight("CGPreflightPostEventAccess")
screen_recording_granted = ChildPreflight("CGPreflightScreenCaptureAccess")


def cmux_installed() -> bool:
    from conductor.cmux_setup import find_executable
    return find_executable() is not None


class MicMeter:
    """Owns the input stream and the loudness the bars draw from.

    Opening the stream is also the microphone *ask*: macOS prompts on the
    first read, so `open` doubles as the permission request. `level` is
    0.0-1.0, eased the same way voice_agent.py's Ui feeds the overlay.
    """

    def __init__(self) -> None:
        self.level = 0.0
        self.flowing = False
        self.stream = None
        self._lock = threading.Lock()

    def open(self) -> bool:
        if self.stream is not None:
            return True
        import numpy as np
        import sounddevice as sd

        def on_block(indata, frames, tinfo, status) -> None:
            rms = float(np.sqrt(np.mean(
                (indata.astype(np.float32) / 32768.0) ** 2)))
            with self._lock:
                self.flowing = True
                self.level = min(1.0, rms * 14.0)

        try:
            self.stream = sd.InputStream(samplerate=24000, channels=1,
                                         dtype="int16", blocksize=480,
                                         callback=on_block)
            self.stream.start()
        except Exception as exc:
            # No device, or the grant refused. The screen keeps polling,
            # so a grant given later still lets the flow move on.
            print(f"microphone not available yet: {exc}", file=sys.stderr)
            self.stream = None
            return False
        return True

    def granted(self) -> bool:
        self.open()
        with self._lock:
            return self.flowing

    def read(self) -> float:
        with self._lock:
            return self.level

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None


class LiveTranscript:
    """The user's words as they are heard, from Apple's on-device
    recognizer.

    The mic test's second witness: the meter proves sound is arriving,
    the words prove it is intelligible. Opening is also the Speech
    Recognition *ask* - macOS prompts on the first request - and a
    machine without the recognizer (or a denied grant) simply reads as
    empty, so the meter alone still carries the test.
    """

    def __init__(self) -> None:
        self.text = ""
        self.failed = False
        self.engine = None
        self.request = None
        self.task = None
        self._lock = threading.Lock()

    def open(self) -> bool:
        if self.engine is not None:
            return True
        if self.failed:
            return False
        try:
            import AVFoundation
            import Speech

            recognizer = Speech.SFSpeechRecognizer.alloc().init()
            if recognizer is None or not recognizer.isAvailable():
                raise RuntimeError("no speech recognizer for this locale")
            Speech.SFSpeechRecognizer.requestAuthorization_(lambda s: None)
            request = (Speech.SFSpeechAudioBufferRecognitionRequest
                       .alloc().init())
            request.setShouldReportPartialResults_(True)
            if recognizer.supportsOnDeviceRecognition():
                request.setRequiresOnDeviceRecognition_(True)

            def on_result(result, error) -> None:
                if result is not None:
                    words = str(result.bestTranscription().formattedString())
                    with self._lock:
                        self.text = words

            engine = AVFoundation.AVAudioEngine.alloc().init()
            node = engine.inputNode()
            node.installTapOnBus_bufferSize_format_block_(
                0, 1024, node.outputFormatForBus_(0),
                lambda buffer, when: request.appendAudioPCMBuffer_(buffer))
            engine.prepare()
            ok, error = engine.startAndReturnError_(None)
            if not ok:
                raise RuntimeError(str(error))
            self.task = recognizer.recognitionTaskWithRequest_resultHandler_(
                request, on_result)
            self.engine, self.request = engine, request
        except Exception as exc:
            # The meter still carries the mic test; only the words go.
            print(f"live transcript not available: {exc}", file=sys.stderr)
            self.failed = True
            return False
        return True

    def read(self) -> str:
        # The recognizer delivers its results through the run loop; a
        # terminal front end has nothing else pumping it, so reading is
        # also a (brief) turn of the loop.
        if self.engine is not None:
            from Foundation import NSDate, NSRunLoop
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.01))
        with self._lock:
            return self.text

    def close(self) -> None:
        if self.engine is None:
            return
        try:
            self.engine.inputNode().removeTapOnBus_(0)
            self.engine.stop()
            self.request.endAudio()
            if self.task is not None:
                self.task.cancel()
        finally:
            self.engine = self.request = self.task = None


def key_down(key: str) -> bool:
    import Quartz
    state = getattr(Quartz, "kCGEventSourceStateHIDSystemState", 1)
    try:
        return key_held(Quartz.CGEventSourceFlagsState(state), key)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The AppKit shell: one window, one card, redrawn per step.

def main() -> int:
    import objc

    from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                        NSAttributedString, NSBackingStoreBuffered,
                        NSBezierPath, NSButton, NSColor, NSFont,
                        NSFontAttributeName, NSForegroundColorAttributeName,
                        NSFontManager, NSGraphicsContext, NSItalicFontMask,
                        NSKernAttributeName, NSMakeRect,
                        NSMakeSize, NSMutableAttributedString,
                        NSMutableParagraphStyle,
                        NSParagraphStyleAttributeName, NSShadow,
                        NSStringDrawingUsesLineFragmentOrigin,
                        NSTextAlignmentCenter, NSView, NSWindow,
                        NSWindowStyleMaskClosable, NSWindowStyleMaskTitled)
    from CoreText import (CTFontManagerRegisterFontsForURL,
                          kCTFontManagerScopeProcess)
    from Foundation import NSObject, NSTimer, NSURL

    # Wispr Flow sets its screens in Figtree with EB Garamond italic for
    # the emphasized phrase; both are bundled (SIL OFL, fonts/) and
    # registered for this process. A missing file falls back to the
    # system face so the flow never dies over typography.
    for rel in ("fonts/figtree/Figtree-Variable.ttf",
                "fonts/figtree/Figtree-Italic-Variable.ttf",
                "fonts/ebgaramond/EBGaramond-Italic-Variable.ttf"):
        font_path = Path(__file__).resolve().parent / rel
        if font_path.exists():
            ok, err = CTFontManagerRegisterFontsForURL(
                NSURL.fileURLWithPath_(str(font_path)),
                kCTFontManagerScopeProcess, None)
            if not ok:
                print(f"could not register {rel}: {err}", file=sys.stderr)

    def rgb(r: int, g: int, b: int, a: float = 1.0):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(
            r / 255.0, g / 255.0, b / 255.0, a)

    # Wispr Flow's palette: warm paper, charcoal ink, one purple accent.
    BG = rgb(238, 235, 227)
    NAVBG = rgb(255, 255, 255)
    INK = rgb(32, 33, 36)
    DIM = rgb(112, 114, 120)
    PURPLE = rgb(139, 124, 246)          # the overlay's "speaking" purple
    BORDER = rgb(222, 219, 211)
    CARD = rgb(255, 255, 255)

    W, HGT = 1240.0, 800.0
    NAV_H = 48.0
    BARS, BAR_W, BAR_GAP = 10, 7.0, 7.0  # the pill's bars, scaled up to read
    BAR_MAX = 72.0                       # from across the room
    CARD_W_, CARD_H, CARD_TOP, CARD_HEAD = 490.0, 300.0, 330.0, 52.0

    def italic(font):
        return NSFontManager.sharedFontManager().convertFont_toHaveTrait_(
            font, NSItalicFontMask)

    def face(name, size, fallback_weight, fallback_italic=False):
        font = NSFont.fontWithName_size_(name, size)
        if font is not None:
            return font
        font = NSFont.systemFontOfSize_weight_(size, fallback_weight)
        return italic(font) if fallback_italic else font

    def figtree(size, weight=0.0):
        name = ("Figtree-Bold" if weight >= 0.5 else
                "Figtree-SemiBold" if weight >= 0.3 else
                "Figtree-Medium" if weight >= 0.15 else "Figtree-Regular")
        return face(name, size, weight)

    def garamond_italic(size):
        return face("EBGaramond-SemiBoldItalic", size, 0.35, True)

    NAV = STAGES

    meter = MicMeter()
    flow = OnboardingFlow({
        "input_monitoring": input_monitoring_granted,
        "microphone": meter.granted,
        "accessibility": accessibility_granted,
        "screen_recording": screen_recording_granted,
    }, key=chosen_key())

    class CardView(NSView):
        def initWithFrame_(self, frame):
            self = objc.super(CardView, self).initWithFrame_(frame)
            if self is None:
                return None
            self.wave = [0.05] * BARS
            self.phase = 0.0
            return self

        def isFlipped(self):
            return True

        def drawRect_(self, _rect):
            bounds = self.bounds()
            w = bounds.size.width
            BG.set()
            NSBezierPath.bezierPathWithRect_(bounds).fill()
            self.drawWatermark(w)
            self.drawNav(w)
            title, body = (part.format(key=KEY_LABELS[flow.key])
                           for part in COPY[flow.step])
            self.drawTitle(title, w)
            hint = ("No bars? Pick an input in System Settings > Sound, "
                    "then speak again." if flow.denied and
                    flow.step == "mic_test" else
                    "Not lighting up? Grant Input Monitoring and restart "
                    "this terminal." if flow.denied else body)
            self.drawText(hint, 16.0, 0.0, DIM, 236.0, w)
            if flow.step in ("welcome", "done"):
                self.drawHero(w)
            if flow.step == "mic_test":
                self.drawCard(w)
                self.drawBars(w)
            if flow.step == "hotkey_test":
                self.drawCard(w)
                self.drawKey(w)

        @objc.python_method
        def drawWatermark(self, w):
            # Wispr's canvas carries its logotype as huge, barely-there
            # script glyphs; ours carries the same gesture.
            font = garamond_italic(620.0)
            for glyph, x, y in (("v", -140.0, 180.0), ("c", w - 420.0, 320.0)):
                NSAttributedString.alloc().initWithString_attributes_(
                    glyph, {NSFontAttributeName: font,
                            NSForegroundColorAttributeName:
                                rgb(32, 33, 36, 0.03)}
                ).drawAtPoint_((x, y))

        # PyObjC bridges every method on an NSView subclass as a selector,
        # and a leading-underscore multi-argument name breaks the bridge
        # (see the note in overlay.py). python_method keeps these plain.
        @objc.python_method
        def drawNav(self, w):
            NAVBG.set()
            NSBezierPath.bezierPathWithRect_(
                NSMakeRect(0, 0, w, NAV_H)).fill()
            BORDER.set()
            NSBezierPath.bezierPathWithRect_(
                NSMakeRect(0, NAV_H - 1, w, 1)).fill()
            active = next(i for i, (_, steps) in enumerate(NAV)
                          if flow.step in steps)
            font = figtree(11.0, 0.3)
            sep = NSAttributedString.alloc().initWithString_attributes_(
                "\u203a", {NSFontAttributeName: font,
                           NSForegroundColorAttributeName: BORDER})
            pieces = []
            for i, (label, _) in enumerate(NAV):
                pieces.append((NSAttributedString.alloc()
                               .initWithString_attributes_(label, {
                                   NSFontAttributeName: font,
                                   NSKernAttributeName: 1.4,
                                   NSForegroundColorAttributeName:
                                       INK if i <= active else DIM,
                               }), i))
            gap = 26.0
            total = (sum(p.size().width for p, _ in pieces)
                     + sep.size().width * (len(pieces) - 1)
                     + 2 * gap * (len(pieces) - 1))
            x = (w - total) / 2
            reached = 0.0
            for n, (piece, i) in enumerate(pieces):
                pw = piece.size().width
                piece.drawAtPoint_((x, (NAV_H - 14.0) / 2))
                if i == active:
                    reached = x + pw
                x += pw + gap
                if n < len(pieces) - 1:
                    sep.drawAtPoint_((x, (NAV_H - 14.0) / 2))
                    x += sep.size().width + gap
            # The underline runs from the left edge through the active
            # stage, the way Wispr's rail keeps the travelled part lit.
            PURPLE.set()
            NSBezierPath.bezierPathWithRect_(
                NSMakeRect(0, NAV_H - 2.0, reached, 2.0)).fill()

        @objc.python_method
        def drawTitle(self, string, w):
            style = NSMutableParagraphStyle.alloc().init()
            style.setAlignment_(NSTextAlignmentCenter)
            text = NSAttributedString.alloc().initWithString_attributes_(
                string, {NSFontAttributeName: figtree(38.0, 0.55),
                         NSForegroundColorAttributeName: INK,
                         NSParagraphStyleAttributeName: style})
            text.drawWithRect_options_(
                NSMakeRect(110.0, 160.0, w - 220.0, 120.0),
                NSStringDrawingUsesLineFragmentOrigin)

        @objc.python_method
        def drawText(self, string, size, weight, color, y, w):
            # Wrapped, not drawn as one line: the welcome body is wider
            # than the window.
            style = NSMutableParagraphStyle.alloc().init()
            style.setAlignment_(NSTextAlignmentCenter)
            attrs = {NSFontAttributeName: figtree(size, weight),
                     NSForegroundColorAttributeName: color,
                     NSParagraphStyleAttributeName: style}
            text = NSAttributedString.alloc().initWithString_attributes_(
                string, attrs)
            margin = 110.0
            text.drawWithRect_options_(
                NSMakeRect(margin, y, w - 2 * margin, 200.0),
                NSStringDrawingUsesLineFragmentOrigin)

        @objc.python_method
        def drawHero(self, w):
            # An ambient waveform, breathing on its own: the product in one
            # picture before any of it is explained.
            self.phase += 0.09
            n, bw, gap = 24, 7.0, 8.0
            span = n * bw + (n - 1) * gap
            start, cy = (w - span) / 2, 480.0
            for i in range(n):
                s = (math.sin(self.phase + i * 0.55) * 0.5 + 0.5) * \
                    (math.sin(self.phase * 0.33 + i * 0.21) * 0.5 + 0.5)
                bar_h = 10.0 + s * 74.0
                PURPLE.colorWithAlphaComponent_(0.35 + 0.65 * s).set()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(start + i * (bw + gap), cy - bar_h / 2,
                               bw, bar_h), bw / 2, bw / 2).fill()

        @objc.python_method
        def drawCard(self, w):
            # The white panel the live feedback sits on, headed by an
            # app-window strip the way Wispr frames its demo cards.
            rect = NSMakeRect((w - CARD_W_) / 2, CARD_TOP, CARD_W_, CARD_H)
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                rect, 16.0, 16.0)
            NSGraphicsContext.currentContext().saveGraphicsState()
            shadow = NSShadow.alloc().init()
            shadow.setShadowColor_(rgb(32, 33, 36, 0.14))
            shadow.setShadowBlurRadius_(26.0)
            shadow.setShadowOffset_(NSMakeSize(0.0, -8.0))
            shadow.set()
            CARD.set()
            path.fill()
            NSGraphicsContext.currentContext().restoreGraphicsState()
            NSGraphicsContext.currentContext().saveGraphicsState()
            path.addClip()
            rgb(246, 246, 246).set()
            NSBezierPath.bezierPathWithRect_(NSMakeRect(
                rect.origin.x, CARD_TOP, CARD_W_, CARD_HEAD)).fill()
            NSGraphicsContext.currentContext().restoreGraphicsState()
            PURPLE.set()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(
                rect.origin.x + 22.0, CARD_TOP + CARD_HEAD / 2 - 6.0,
                12.0, 12.0)).fill()
            NSAttributedString.alloc().initWithString_attributes_(
                "Voice Conductor",
                {NSFontAttributeName: figtree(13.0, 0.4),
                 NSForegroundColorAttributeName: INK}
            ).drawAtPoint_((rect.origin.x + 44.0,
                            CARD_TOP + CARD_HEAD / 2 - 8.0))
            BORDER.set()
            path.setLineWidth_(1.0)
            path.stroke()

        @objc.python_method
        def drawBars(self, w):
            self.wave = self.wave[1:] + [max(0.05, meter.read())]
            span = BARS * BAR_W + (BARS - 1) * BAR_GAP
            start = (w - span) / 2
            cy = CARD_TOP + CARD_HEAD + (CARD_H - CARD_HEAD) / 2
            PURPLE.set()
            for i, sample in enumerate(self.wave):
                bar_h = max(6.0, sample * BAR_MAX)
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(start + i * (BAR_W + BAR_GAP),
                               cy - bar_h / 2, BAR_W, bar_h),
                    BAR_W / 2, BAR_W / 2).fill()

        @objc.python_method
        def drawKey(self, w):
            side = 110.0
            held = key_down(flow.key)
            top = CARD_TOP + CARD_HEAD + (CARD_H - CARD_HEAD - side) / 2
            rect = NSMakeRect((w - side) / 2, top, side, side)
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                rect, 18.0, 18.0)
            (PURPLE if held else BG).set()
            path.fill()
            (PURPLE if held else BORDER).set()
            path.setLineWidth_(1.5)
            path.stroke()
            self.drawText(KEY_GLYPHS[flow.key], 32.0, 0.35,
                          NAVBG if held else INK, top + side / 2 - 22.0, w)

    class Controller(NSObject):
        def init(self):
            self = objc.super(Controller, self).init()
            if self is None:
                return None
            self.window = NSWindow.alloc(
            ).initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, W, HGT),
                NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
                NSBackingStoreBuffered, False)
            self.window.setTitle_("Voice Conductor")
            self.window.setTitlebarAppearsTransparent_(True)
            self.window.setBackgroundColor_(NAVBG)
            self.card = CardView.alloc().initWithFrame_(
                NSMakeRect(0, 0, W, HGT))
            self.yes = self.makeButton("Continue", "confirm:", True)
            self.no = self.makeButton("No", "deny:", False)
            self.backer = NSButton.buttonWithTitle_target_action_(
                "", self, "goBack:")
            self.backer.setBordered_(False)
            style = NSMutableParagraphStyle.alloc().init()
            style.setAlignment_(NSTextAlignmentCenter)
            self.backer.setAttributedTitle_(
                NSAttributedString.alloc().initWithString_attributes_(
                    "\u2190 Back",
                    {NSFontAttributeName: figtree(14.0, 0.2),
                     NSForegroundColorAttributeName: DIM,
                     NSParagraphStyleAttributeName: style}))
            self.keys = []
            for i, name in enumerate(KEY_MASKS):
                chip = self.makeButton("", "pick:", False)
                chip.layer().setCornerRadius_(14.0)
                self.rekeycap(chip, name)
                chip.setTag_(i)
                self.keys.append(chip)
            content = self.window.contentView()
            content.addSubview_(self.card)
            content.addSubview_(self.yes)
            content.addSubview_(self.no)
            content.addSubview_(self.backer)
            for chip in self.keys:
                content.addSubview_(chip)
            self.window.center()
            self.window.makeKeyAndOrderFront_(None)
            self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / 20.0, self, "tick:", None, True)
            self.relayout()
            return self

        @objc.python_method
        def makeButton(self, label, action, primary):
            button = NSButton.buttonWithTitle_target_action_(
                label, self, action)
            button.setBordered_(False)
            button.setWantsLayer_(True)
            layer = button.layer()
            layer.setCornerRadius_(10.0)
            if primary:
                layer.setBackgroundColor_(PURPLE.CGColor())
            else:
                layer.setBackgroundColor_(NAVBG.CGColor())
                layer.setBorderWidth_(1.0)
                layer.setBorderColor_(BORDER.CGColor())
            self.retitle(button, label, primary)
            return button

        @objc.python_method
        def retitle(self, button, label, primary):
            style = NSMutableParagraphStyle.alloc().init()
            style.setAlignment_(NSTextAlignmentCenter)
            attrs = {NSFontAttributeName: figtree(14.0, 0.3),
                     NSForegroundColorAttributeName:
                         NAVBG if primary else INK,
                     NSParagraphStyleAttributeName: style}
            button.setAttributedTitle_(
                NSAttributedString.alloc().initWithString_attributes_(
                    label, attrs))

        @objc.python_method
        def rekeycap(self, button, name):
            # A keycap card: the glyph large, the key's name under it.
            style = NSMutableParagraphStyle.alloc().init()
            style.setAlignment_(NSTextAlignmentCenter)
            style.setParagraphSpacingBefore_(6.0)
            text = NSMutableAttributedString.alloc().initWithString_(
                f"{KEY_GLYPHS[name]}\n{KEY_LABELS[name]}")
            text.addAttributes_range_(
                {NSFontAttributeName: figtree(34.0, 0.3),
                 NSForegroundColorAttributeName: INK,
                 NSParagraphStyleAttributeName: style},
                (0, len(KEY_GLYPHS[name])))
            text.addAttributes_range_(
                {NSFontAttributeName: figtree(13.0, 0.25),
                 NSForegroundColorAttributeName: DIM,
                 NSParagraphStyleAttributeName: style},
                (len(KEY_GLYPHS[name]) + 1, len(KEY_LABELS[name])))
            button.setAttributedTitle_(text)

        @objc.python_method
        def relayout(self):
            step = flow.step
            question = step in ("mic_test", "hotkey_test")
            self.retitle(self.yes, "Yes" if question else
                         "Finish" if step == "done" else "Continue", True)
            picking = step == "choose_key"
            self.yes.setHidden_(step in OnboardingFlow.AUTO or picking)
            self.no.setHidden_(not question)
            self.yes.setFrame_(NSMakeRect(
                W / 2 + (10 if question else -90), 72, 180, 48))
            self.no.setFrame_(NSMakeRect(W / 2 - 190, 72, 180, 48))
            self.backer.setHidden_(
                step in ("welcome", "done") + OnboardingFlow.AUTO)
            self.backer.setFrame_(NSMakeRect(
                44, HGT - NAV_H - 64, 80, 28))
            chip_w, chip_gap = 150.0, 20.0
            row = len(self.keys) * chip_w + (len(self.keys) - 1) * chip_gap
            for i, chip in enumerate(self.keys):
                chip.setHidden_(not picking)
                chip.setFrame_(NSMakeRect(
                    (W - row) / 2 + i * (chip_w + chip_gap), 320,
                    chip_w, 130))
            if step in PANES:
                open_pane(step)
            self.card.setNeedsDisplay_(True)

        def applicationShouldTerminateAfterLastWindowClosed_(self, _app):
            # Closing the window is quitting: the launchers wait on this
            # process, so it must not linger windowless.
            return True

        def applicationWillTerminate_(self, _note):
            meter.close()

        def confirm_(self, _sender):
            if flow.finished:
                meter.close()
                NSApplication.sharedApplication().terminate_(None)
                return
            flow.confirm()
            self.relayout()

        def deny_(self, _sender):
            flow.deny()
            self.card.setNeedsDisplay_(True)

        def goBack_(self, _sender):
            flow.back()
            self.relayout()

        def pick_(self, sender):
            key = list(KEY_MASKS)[sender.tag()]
            save_key(key)
            print(f"push-to-talk key saved: {KEY_LABELS[key]} -> "
                  f"{CONFIG_PATH}", file=sys.stderr)
            flow.choose(key)
            self.relayout()

        def tick_(self, _timer):
            if flow.poll():
                self.relayout()
            if flow.step in ("mic_test", "hotkey_test"):
                meter.open()
                self.card.setNeedsDisplay_(True)
            elif flow.step in ("welcome", "done"):
                # The ambient waveform breathes on the same clock.
                self.card.setNeedsDisplay_(True)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    controller = Controller.alloc().init()
    app.setDelegate_(controller)
    app.activateIgnoringOtherApps_(True)
    assert controller is not None
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
