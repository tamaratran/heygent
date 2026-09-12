"""Talking to the user when there is no terminal to talk on.

heygent.app is opened from Finder: its stdout and stderr go to
~/.voice-conductor/logs/app-launch.log, which nobody is reading while
they wait for the app to say something. Anything the user has to act on
- a key that was refused, a sign-in that is missing, a permission to turn
on - therefore goes to a dialog when stdin is not a terminal, and to the
terminal when it is. The dialogs are macOS's own (`display dialog` via
osascript); on another platform, or without osascript, only the terminal
is left and that is where the words go.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .app_bundle import make_icns
from .observability import application_log

TITLE = "heygent"

# `display dialog` from a script: the text and the buttons come in as
# arguments, so nothing the user typed is ever spliced into a script.
_DIALOG = '''on run argv
  set message to item 1 of argv
  set labels to rest of argv
  set answer to display dialog message with title "%s" buttons labels default button (last item of labels) with icon caution
  return button returned of answer
end run''' % TITLE

# The icon: heygent's own when its .icns can be found (item 2, a path or
# ""), else the stock note icon. A third button (item 3, or "") is
# reported by name with an empty answer, so the caller can act on it and
# ask again.
_SECRET = '''on run argv
  set message to item 1 of argv
  set icns to item 2 of argv
  set extra to item 3 of argv
  set labels to {"Quit", "Save"}
  if extra is not "" then set labels to {"Quit", extra, "Save"}
  if icns is "" then
    set answer to display dialog message with title "%s" default answer "" with hidden answer buttons labels default button "Save" with icon note
  else
    set answer to display dialog message with title "%s" default answer "" with hidden answer buttons labels default button "Save" with icon POSIX file icns
  end if
  if button returned of answer is "Quit" then return ""
  if button returned of answer is extra then return "button:" & extra
  return text returned of answer
end run''' % (TITLE, TITLE)


def icon_file(home: Path | None = None) -> str:
    """heygent.icns: the outer app bundle's when the code runs inside one
    (Contents/Resources/app/..), else the one the Dock window keeps in the
    conductor home, made from assets/icon.png when neither exists yet.
    "" when there is no icon to be had."""
    here = Path(__file__).resolve().parent.parent
    outer = here.parent / "heygent.icns"
    if outer.is_file():
        return str(outer)
    home = home or Path(os.environ.get("VOICE_CONDUCTOR_HOME")
                        or Path.home() / ".voice-conductor")
    kept = home / "heygent.app" / "Contents" / "Resources" / "heygent.icns"
    if kept.is_file():
        return str(kept)
    kept.parent.mkdir(parents=True, exist_ok=True)
    return str(kept) if make_icns(here / "assets" / "icon.png", kept) else ""


def has_terminal() -> bool:
    """Whether a person is on the other end of stdin."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def can_show(platform: str | None = None) -> bool:
    return (platform or sys.platform) == "darwin"


def _osascript(script: str, *args: str, run=subprocess.run) -> str | None:
    """Run a script; its stdout, or None when it could not run or the
    user cancelled (osascript exits 1 on a cancelled dialog)."""
    try:
        done = run(["osascript", "-", *args], input=script,
                   capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired):
        application_log("ui", "dialog.failed", "could not show a dialog",
                        severity="warning", exc_info=True)
        return None
    if done.returncode != 0:
        return None
    return done.stdout.rstrip("\n")


def alert(text: str, buttons: tuple[str, ...] = ("OK",),
          *, run=subprocess.run) -> str | None:
    """A dialog with the given buttons; the one clicked, None when the
    dialog could not be shown or was cancelled."""
    if not can_show():
        return None
    return _osascript(_DIALOG, text, *buttons, run=run)


def ask_secret(text: str, *, button: str = "", icon: str | None = None,
               run=subprocess.run) -> str:
    """A hidden-answer dialog; "" when cancelled or unavailable. With a
    `button`, pressing it returns "button:<label>" instead of an answer."""
    if not can_show():
        return ""
    icon = icon_file() if icon is None else icon
    return (_osascript(_SECRET, text, icon, button, run=run) or "").strip()


def tell(text: str, *, run=subprocess.run) -> None:
    """Say something the user must act on: on the terminal when there is
    one, otherwise in a dialog (and in the log either way, via stderr)."""
    print(text, file=sys.stderr, flush=True)
    if not has_terminal():
        alert(text, run=run)


STOPPED = ("heygent stopped: {reason}\n\nThis was not supposed to happen. "
           "The log has the details - Show Log opens it; sending it along "
           "with a report helps.")


def stopped(reason: str, log_path: str | Path | None, *,
            run=subprocess.run) -> None:
    """The app is quitting for a reason nothing else has explained. On a
    terminal the traceback that follows says it; from Finder this
    dialog is the only place it can be said, with the log a click
    away."""
    if has_terminal():
        return
    text = STOPPED.format(reason=reason)
    if log_path is not None:
        text += f"\n\n{log_path}"
    if alert(text, ("Quit", "Show Log"), run=run) == "Show Log" and log_path:
        open_url(str(log_path), run=run)


def open_url(url: str, *, run=subprocess.run) -> bool:
    try:
        done = run(["open", url], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0
