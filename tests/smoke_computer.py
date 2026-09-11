"""The computer driver against the real screen - macOS only, by hand.

Opens a fresh TextEdit document, types into it through CGEvents, pastes
into it through the clipboard, and reads the document back through
AppleScript to prove the keystrokes landed where they were aimed - and
that the clipboard held what it held before the paste. Separate from the unit suite
because it needs the Accessibility and Screen Recording permissions and
takes over the GUI for a few seconds.

Run with:  python3 -u tests/smoke_computer.py

It drives the Driver directly, so it exercises the event grammar and not
the two checks the command-line driver makes first: the GUI lease (is a
live conductor still supervising this worker) and the screen guard (is
the screen still what we last looked at). Those are unit-tested in
tests/test_one_conductor and tests/test_two_agents_one_keyboard, because
they are about a machine somebody else is also using and a smoke test
that took the screen would be that somebody.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conductor.computer import Driver  # noqa: E402

SENTENCE = "the computer driver was here"
PASTED = " and pasted this"


def osascript(script: str) -> str:
    result = subprocess.run(["osascript", "-e", script],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"osascript failed: {result.stderr.strip()}")
    return result.stdout.strip()


def main() -> int:
    driver = Driver()
    permissions = driver.permissions()
    print(f"permissions: {permissions}")
    missing = [name for name, granted in permissions.items() if not granted]
    if missing:
        print(f"grant {', '.join(missing)} in System Settings > Privacy & "
              "Security, then rerun")
        return 1

    shot = driver.screenshot()
    print(f"screenshot: {shot} ({shot.stat().st_size} bytes)")

    print("opening a fresh TextEdit document...")
    osascript('tell application "TextEdit" to activate')
    osascript('tell application "TextEdit" to make new document')
    time.sleep(1.0)

    print(f"typing: {SENTENCE!r}")
    driver.type_text(SENTENCE)
    time.sleep(0.5)

    text = osascript('tell application "TextEdit" to get text of document 1')
    print(f"TextEdit now holds: {text!r}")

    ok = text == SENTENCE
    print("typed text landed" if ok else "MISMATCH: keystrokes went "
                                         "somewhere else")

    print(f"apps says: {driver.running_apps()[0]}")
    print(f"pasting: {PASTED!r}")
    clipboard_before = driver.clipboard.snapshot()[0]
    restored, _ = driver.paste_text(PASTED)
    time.sleep(0.5)
    text = osascript('tell application "TextEdit" to get text of document 1')
    pasted = text == SENTENCE + PASTED
    print("pasted text landed" if pasted else
          f"MISMATCH: TextEdit holds {text!r}")
    clipboard_back = restored and \
        driver.clipboard.snapshot()[0] == clipboard_before
    print("clipboard put back" if clipboard_back else
          "MISMATCH: the clipboard is not what it was")
    ok = ok and pasted and clipboard_back

    print("pressing cmd+a then delete to clear it...")
    driver.press("cmd+a")
    driver.press("delete")
    time.sleep(0.5)
    cleared = osascript('tell application "TextEdit" to get text of '
                        'document 1') == ""
    print("chord landed" if cleared else "MISMATCH: cmd+a/delete did not "
                                         "clear the document")

    osascript('tell application "TextEdit" to close document 1 '
              'saving no')
    print("done" if ok and cleared else "FAILED")
    return 0 if ok and cleared else 1


if __name__ == "__main__":
    sys.exit(main())
