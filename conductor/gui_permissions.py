"""Asking for the macOS permissions this app needs - at start, once each.

Four grants, two of them for the voice itself: Microphone (to hear you)
and Input Monitoring (to see the Fn key), then Accessibility and Screen
Recording for computer use. macOS never asks for Accessibility, Screen
Recording or a denied Microphone on its own. Without them nothing fails
loudly: a posted click is silently dropped, a screenshot comes back as an
empty desktop, the Fn key is simply never seen. Until now the way to find
out was a worker that could not act, and the way to fix it was a pane we
opened by hand with `open x-apple.systempreferences:...`.

So the conductor asks as it starts. It probes what its own app - the
terminal running conduct.sh, which is where the capability probe runs -
has been granted, and for each grant that is missing it opens the exact
Privacy & Security pane and says in plain words which apps to add there:
this terminal, and the app that hosts the workers (cmux, usually) if it
is a different one. macOS attributes a grant to the app that owns the
process, so both need it; only the first can be probed from here.

Once per missing grant, not on every launch: each ask is remembered in
the conductor home, and forgotten again when the grant appears, so a
permission revoked later is asked for once more. Everything already
granted, or any other platform, and this does nothing at all.

The very first launch also introduces the grants before any of that: a
banner naming each permission this app uses, what it is for, and whether
it is needed always or only for computer use - so the Settings panes that
follow arrive announced. Shown once per set of grants, remembered in the
same file as the asks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import computer
from .observability import application_log

# The name macOS shows for the app bundle, in the Dock and in every
# Privacy & Security list.
OWN_APP = "heygent"

# The pane for each grant. Voice grants first: without the microphone
# nothing else matters.
PANES = {
    "microphone": ("x-apple.systempreferences:com.apple.preference."
                   "security?Privacy_Microphone"),
    "input_monitoring": ("x-apple.systempreferences:com.apple.preference."
                         "security?Privacy_ListenEvent"),
    "accessibility": ("x-apple.systempreferences:com.apple.preference."
                      "security?Privacy_Accessibility"),
    "screen_recording": ("x-apple.systempreferences:com.apple.preference."
                         "security?Privacy_ScreenCapture"),
}
LABELS = {"microphone": "Microphone",
          "input_monitoring": "Input Monitoring",
          "accessibility": "Accessibility",
          "screen_recording": "Screen Recording"}
PURPOSES = {"microphone": "to hear you",
            "input_monitoring": "to see the Fn key",
            "accessibility": "to click and type",
            "screen_recording": "to take screenshots"}
# Whose need each grant is, for the sentence.
NEEDS = {"microphone": "Voice", "input_monitoring": "The Fn hotkey",
         "accessibility": "Computer use", "screen_recording": "Computer use"}
# The grants the worker app (cmux) also has to hold; the voice grants
# belong to the terminal alone.
COMPUTER_GRANTS = ("accessibility", "screen_recording")
VOICE_GRANTS = ("microphone", "input_monitoring")
# What has to be restarted for the grant to take effect, from a terminal.
RESTARTS = {"microphone": "restart conduct.sh",
            "input_monitoring": "restart your terminal (macOS applies "
                                "Input Monitoring only to new processes)",
            "accessibility": "restart conduct.sh",
            "screen_recording": "restart conduct.sh"}
# The same, when the app itself is what was opened.
APP_RESTARTS = {"microphone": f"quit {OWN_APP} and open it again",
                "input_monitoring": f"quit {OWN_APP} and open it again "
                                    "(macOS applies Input Monitoring only "
                                    "to newly started apps)",
                "accessibility": f"quit {OWN_APP} and open it again",
                "screen_recording": f"quit {OWN_APP} and open it again"}
# The dialog's buttons, when there is no terminal to print on.
LATER, OPEN_SETTINGS, QUIT = "Later", "Open Settings", "Quit"

STATE_FILE = "gui-permissions.json"

# When each grant matters, for the first-launch banner.
WHEN_NEEDED = {
    "microphone": "needed always",
    "input_monitoring": "needed always",
    "accessibility": "only for computer-use tasks",
    "screen_recording": "only for computer-use tasks",
}

# The banner's dress. Worn only when stdout is a terminal that wants it.
_STYLES = {"border": "\x1b[38;5;73m", "title": "\x1b[1m",
           "dim": "\x1b[2m", "reset": "\x1b[0m"}
# OSC 8 hyperlink: terminals that know it make the text clickable,
# the rest show the text alone.
_LINK, _LINK_END = "\x1b]8;;{url}\x1b\\", "\x1b]8;;\x1b\\"

# macOS hands every process the bundle id of the app it was launched
# from, which is exactly the app the grant is attributed to.
APP_NAMES = {
    "ai.heygent.conductor": "heygent",
    "com.apple.Terminal": "Terminal",
    "com.googlecode.iterm2": "iTerm2",
    "com.mitchellh.ghostty": "Ghostty",
    "com.cmuxterm.app": "cmux",
    "dev.warp.Warp-Stable": "Warp",
    "net.kovidgoyal.kitty": "kitty",
    "org.alacritty": "Alacritty",
    "com.github.wez.wezterm": "WezTerm",
    "com.microsoft.VSCode": "Visual Studio Code",
    "com.todesktop.230313mzl4w4u92": "Cursor",
}
TERM_PROGRAM_NAMES = {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm2",
                      "vscode": "Visual Studio Code", "WarpTerminal": "Warp",
                      "ghostty": "Ghostty"}
UNKNOWN_APP = "the terminal app running conduct.sh"


@dataclass(frozen=True)
class Ask:
    """One pane opened for one missing grant, and what the user was told."""
    grant: str
    opened: bool
    message: str


def this_app(env=None) -> str:
    """The app macOS attributes this process to, by the name it shows in
    the Privacy & Security list."""
    env = os.environ if env is None else env
    bundle = env.get("__CFBundleIdentifier", "")
    if bundle in APP_NAMES:
        return APP_NAMES[bundle]
    program = env.get("TERM_PROGRAM", "")
    if program:
        return TERM_PROGRAM_NAMES.get(program, program)
    if bundle:
        return bundle.rsplit(".", 1)[-1]
    return UNKNOWN_APP


def probe_voice_grants(wanted=VOICE_GRANTS) -> dict:
    """Microphone and Input Monitoring, probed through the system's own
    answers. None where the answer is unknowable (no bindings, another
    platform): unknown is not missing, macOS will ask on first use.
    """
    grants: dict[str, bool | None] = {}
    if "input_monitoring" in wanted:
        grants["input_monitoring"] = None
        try:
            from Quartz import CGPreflightListenEventAccess
            grants["input_monitoring"] = bool(CGPreflightListenEventAccess())
        except Exception:
            pass
    if "microphone" in wanted:
        grants["microphone"] = None
        try:
            from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
            status = AVCaptureDevice.authorizationStatusForMediaType_(
                AVMediaTypeAudio)
            # 0 is not-determined: macOS itself asks the first time the
            # microphone is opened, so only an explicit denial (2) or a
            # restriction (1) is a pane's problem.
            grants["microphone"] = None if status == 0 else status == 3
        except Exception:
            pass
    return grants


def wants_color(env=None) -> bool:
    """Whether stdout is a terminal that takes ANSI styling."""
    env = os.environ if env is None else env
    try:
        tty = sys.stdout.isatty()
    except Exception:
        tty = False
    return tty and not env.get("NO_COLOR") and env.get("TERM") != "dumb"


def first_launch_banner(grants=None, color: bool = False) -> str:
    """The permissions this app uses, introduced before any pane opens:
    each one's name, what it is for, and when it matters. A rounded box
    in the style of the CLIs it launches; plain text without `color`."""
    wanted = tuple(grants) if grants else tuple(PANES)

    def paint(text: str, kind: str) -> str:
        return (f"{_STYLES[kind]}{text}{_STYLES['reset']}"
                if color else text)

    def link(text: str, url: str) -> str:
        return (_LINK.format(url=url) + text + _LINK_END) if color else text

    row_grants = [grant for grant in PANES if grant in wanted]
    label_w = max(len(LABELS[grant]) for grant in row_grants)
    purpose_w = max(len(PURPOSES[grant]) for grant in row_grants)
    # (plain, shown, style): plain carries the width, shown the dress.
    title = "heygent · First launch"
    body = [(title, title, "title"), ("", "", None),
            ("macOS permissions this app uses",) * 2 + (None,),
            ("(System Settings > Privacy & Security):",) * 2 + (None,),
            ("", "", None)]
    for grant in row_grants:
        label = LABELS[grant]
        rest = (f"{' ' * (label_w - len(label))}  "
                f"{PURPOSES[grant]:<{purpose_w}}  {WHEN_NEEDED[grant]}")
        body.append((f"  {label}{rest}",
                     f"  {link(label, PANES[grant])}{rest}", None))
    tail = []
    if "input_monitoring" in wanted:
        tail.append(("Input Monitoring reaches only new processes:",
                     "restart your terminal after granting it."))
    tail.append(("Anything missing is asked for below:",
                 "the exact Settings pane opens for each."))
    for first, second in tail:
        body += [("", "", None), (first, first, "dim"),
                 (second, second, "dim")]
    width = max(len(plain) for plain, _, _ in body)
    edge = paint("│", "border")
    out = [paint("╭" + "─" * (width + 4) + "╮", "border")]
    for plain, shown, kind in body:
        pad = " " * (width - len(plain))
        out.append(f"{edge}  {paint(shown, kind) if kind else shown}"
                   f"{pad}  {edge}")
    out.append(paint("╰" + "─" * (width + 4) + "╯", "border"))
    return "\n".join(out)


def missing_grants(driver_factory=computer.make_driver,
                   voice_probe=probe_voice_grants,
                   grants=None) -> list[str]:
    """The grants this process's app lacks, in pane order.

    No bindings at all is not a missing grant: there is nothing a pane
    could fix, and the capability probe already tells the Manager.
    """
    wanted = tuple(grants) if grants else tuple(PANES)
    granted: dict[str, bool | None] = {}
    voice_wanted = tuple(name for name in VOICE_GRANTS if name in wanted)
    if voice_wanted:
        granted.update(voice_probe(voice_wanted))
    if any(name in wanted for name in COMPUTER_GRANTS):
        try:
            driver = driver_factory()
        except computer.ComputerError:
            pass
        else:
            perms = driver.permissions()
            for name in COMPUTER_GRANTS:
                granted[name] = bool(perms.get(name, False))
    return [name for name in PANES
            if name in wanted and granted.get(name) is False]


def open_pane(url: str) -> bool:
    """Bring System Settings up on one pane. False when it could not."""
    try:
        done = subprocess.run(["open", url], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def register_with_macos(grant: str) -> None:
    """Have macOS list this app in the grant's pane.

    A pane lists only the apps that have asked for its grant; until this
    app has, there is no row to turn on - the user was sent to
    Accessibility and found ChatGPT, iTerm and Terminal there and nothing
    of ours. Accessibility, Input Monitoring and Screen Recording each
    have a call that asks (adding the row, and putting up the system's
    own prompt, which offers the same pane); the Microphone row appears
    the first time the microphone is opened, so it needs nothing here.
    """
    try:
        if grant == "accessibility":
            from ApplicationServices import (AXIsProcessTrustedWithOptions,
                                             kAXTrustedCheckOptionPrompt)
            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        elif grant == "input_monitoring":
            from Quartz import CGRequestListenEventAccess
            CGRequestListenEventAccess()
        elif grant == "screen_recording":
            from Quartz import CGRequestScreenCaptureAccess
            CGRequestScreenCaptureAccess()
    except Exception:
        application_log("conductor", "permissions.request_failed",
                        f"could not ask macOS for {LABELS[grant]}",
                        severity="warning", exc_info=True, grant=grant)


def _load(path: Path) -> tuple[dict, set]:
    """(asked, introduced) as remembered, empty when unreadable."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}, set()
    if not isinstance(data, dict):
        return {}, set()
    asked = data.get("asked")
    introduced = data.get("introduced")
    return (dict(asked) if isinstance(asked, dict) else {},
            set(introduced) if isinstance(introduced, list) else set())


def _save(path: Path, asked: dict, introduced: set) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"asked": asked,
                                "introduced": sorted(introduced)},
                               indent=2, sort_keys=True))


def _apps(grant: str, app: str, worker_app: str | None) -> str:
    # Only computer use runs inside the worker app; the microphone and
    # the hotkey belong to this process alone.
    if worker_app and worker_app != app and grant in COMPUTER_GRANTS:
        return f"{app} and {worker_app} (it runs the workers)"
    return app


def _restart(grant: str, app: str) -> str:
    return (APP_RESTARTS if app == OWN_APP else RESTARTS)[grant]


def _message(grant: str, app: str, worker_app: str | None,
             opened: bool) -> str:
    label = LABELS[grant]
    where = (f"Opening System Settings > Privacy & Security > {label}"
             if opened else
             f"Open System Settings > Privacy & Security > {label}")
    return (f"{NEEDS[grant]} needs {label} {PURPOSES[grant]}, and {app} "
            f"does not have it. {where}: turn it on for "
            f"{_apps(grant, app, worker_app)}, then {_restart(grant, app)}.")


def _dialog_message(grant: str, app: str, worker_app: str | None) -> str:
    """The same ask, for a dialog: what to click, row by row, since the
    person reading it has no terminal and may never have seen the pane."""
    label = LABELS[grant]
    apps = _apps(grant, app, worker_app)
    return (f"{NEEDS[grant]} needs {label} {PURPOSES[grant]}, and {app} "
            f"does not have it yet.\n\n"
            f"In System Settings > Privacy & Security > {label}, turn on "
            f"the switch next to {apps}. If {app} is not in the list, "
            f"click + and choose {app} from Applications.\n\n"
            f"Then {_restart(grant, app)}.")


def _refusal_message(grant: str, app: str, terminal: bool) -> str:
    label = LABELS[grant]
    lead = (f"{NEEDS[grant]} cannot work: macOS refused, because {app} "
            f"does not have {label} {PURPOSES[grant]}.")
    if terminal:
        return (f"{lead} Open System Settings > Privacy & Security > "
                f"{label}, turn it on for {app}, then "
                f"{_restart(grant, app)}.")
    return (f"{lead}\n\n"
            f"In System Settings > Privacy & Security > {label}, turn on "
            f"the switch next to {app}. If {app} is not in the list, "
            f"click + and choose {app} from Applications.\n\n"
            f"{OWN_APP} will quit now; open it again once it is on.")


def explain_refusal(grant: str, *, env=None, opener=open_pane,
                    announce=print, register=register_with_macos,
                    dialog=None) -> Ask:
    """macOS has just said no to `grant` (the hotkey's event tap was
    refused): say so and offer the pane, whatever was asked before.

    ask_for_missing_grants asks once and stays quiet after, which is
    right for a grant the app can do without. This one it cannot - a
    refused tap ends the hotkey, and the app with it - and a person who
    clicked Later, or turned the switch on without restarting, would
    otherwise see the app open and vanish with the reason in a log file.
    """
    app = this_app(env)
    register(grant)
    if dialog is not None:
        message = _refusal_message(grant, app, terminal=False)
        chosen = dialog(message, (QUIT, OPEN_SETTINGS))
        opened = chosen == OPEN_SETTINGS and bool(opener(PANES[grant]))
    else:
        message = _refusal_message(grant, app, terminal=True)
        announce(message)
        opened = bool(opener(PANES[grant]))
    application_log("conductor", "permissions.refused",
                    f"macOS refused {LABELS[grant]} to {app}",
                    severity="error", grant=grant, app=app,
                    pane_opened=opened, dialog=dialog is not None)
    return Ask(grant, opened, message)


def ask_for_missing_grants(home: Path, *, worker_app: str | None = None,
                           platform: str | None = None, env=None,
                           driver_factory=computer.make_driver,
                           voice_probe=probe_voice_grants,
                           grants=None,
                           opener=open_pane, announce=print,
                           register=register_with_macos,
                           dialog=None) -> list[Ask]:
    """Open the pane for each grant this app lacks and has not been asked
    for yet; say which apps to add. Returns what was asked this time.

    worker_app is the app hosting the workers when it is not this one
    (cmux, usually): its grant cannot be probed from here, so it is named
    in the ask rather than checked.

    With `dialog` (text, buttons) -> button, the ask is a dialog instead
    of a printed line - for the app opened from Finder, where print goes
    to a log file - and the pane opens only when the user chooses to.
    """
    if (platform or sys.platform) != "darwin":
        return []
    path = Path(home) / STATE_FILE
    asked, introduced = _load(path)
    before = dict(asked), set(introduced)
    wanted = tuple(grants) if grants else tuple(PANES)
    if not set(wanted) <= introduced:
        announce(first_launch_banner(wanted, color=wants_color(env)))
        application_log("conductor", "permissions.introduced",
                        "introduced the permissions on first launch",
                        grants=list(wanted))
        introduced |= set(wanted)
    missing = missing_grants(driver_factory, voice_probe, wanted)
    for grant in wanted:
        if grant not in missing and grant in asked:
            # Granted since the ask: forget it, so a revocation asks again.
            del asked[grant]
            application_log("conductor", "permissions.granted",
                            f"{LABELS[grant]} is granted now", grant=grant)
    app = this_app(env)
    asks: list[Ask] = []
    for grant in missing:
        if grant in asked:
            application_log("conductor", "permissions.still_missing",
                            f"{LABELS[grant]} is still not granted to "
                            f"{app}; asked on {asked[grant]}",
                            grant=grant, app=app)
            continue
        # Before the pane is on screen: the row it has to show.
        register(grant)
        if dialog is not None:
            message = _dialog_message(grant, app, worker_app)
            chosen = dialog(message, (LATER, OPEN_SETTINGS))
            opened = chosen == OPEN_SETTINGS and bool(opener(PANES[grant]))
        else:
            opened = bool(opener(PANES[grant]))
            message = _message(grant, app, worker_app, opened)
            # The sentence goes to the user through announce; the log
            # keeps the facts (a warning here would print it twice).
            announce(message)
        asked[grant] = time.strftime("%Y-%m-%dT%H:%M:%S")
        application_log("conductor", "permissions.asked",
                        f"asked for {LABELS[grant]} for {app}",
                        grant=grant, app=app, worker_app=worker_app,
                        pane_opened=opened, dialog=dialog is not None)
        asks.append(Ask(grant, opened, message))
    if (asked, introduced) != before:
        _save(path, asked, introduced)
    return asks
