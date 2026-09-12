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

import subprocess
import sys

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

_SECRET = '''on run argv
  set message to item 1 of argv
  set answer to display dialog message with title "%s" default answer "" with hidden answer buttons {"Quit", "Save"} default button "Save" with icon note
  if button returned of answer is "Quit" then return ""
  return text returned of answer
end run''' % TITLE


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


def ask_secret(text: str, *, run=subprocess.run) -> str:
    """A hidden-answer dialog; "" when cancelled or unavailable."""
    if not can_show():
        return ""
    return (_osascript(_SECRET, text, run=run) or "").strip()


def tell(text: str, *, run=subprocess.run) -> None:
    """Say something the user must act on: on the terminal when there is
    one, otherwise in a dialog (and in the log either way, via stderr)."""
    print(text, file=sys.stderr, flush=True)
    if not has_terminal():
        alert(text, run=run)


def open_url(url: str, *, run=subprocess.run) -> bool:
    try:
        done = run(["open", url], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0
