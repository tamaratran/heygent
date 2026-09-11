#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "numpy>=1.26,<3",
#   "sounddevice>=0.4.6,<1",
#   "pyobjc-framework-Quartz>=10,<13",
#   "pyobjc-framework-AVFoundation>=10,<13",
#   "pyobjc-framework-Speech>=10,<13",
# ]
# ///

"""The onboarding flow, entirely in the terminal.

The same steps, probes and step machine as onboarding.py, with the screen
swapped for ANSI: a live level meter instead of the purple bars, a keycap
that fills in text instead of a lit graphic, numbered keys instead of
chips. For machines driven over SSH, screen readers, and anyone who would
rather never leave the terminal.

    ./conduct.sh --onboard-cli

Everything that decides (OnboardingFlow) and everything that senses
(MicMeter, the permission probes) is imported from onboarding.py, so the
two front ends cannot drift apart. Only the rendering lives here, and the
line-producing functions are pure so they are tested without a terminal.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import select
import shutil
import subprocess
import sys
import termios
import textwrap
import time
import tty

from hotkey import KEY_GLYPHS, KEY_LABELS, KEY_MASKS, chosen_key, save_key
from onboarding import (CLI_STAGES, CLI_STEPS, COPY, LiveTranscript,
                        MicMeter, OnboardingFlow, accessibility_granted,
                        claude_logged_in, cmux_installed,
                        input_monitoring_granted, key_down, open_pane,
                        openai_key_saved, save_openai_key,
                        screen_recording_granted)

PURPLE = "\x1b[38;5;141m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"
CLEAR_LINE = "\r\x1b[K"
CHECK = f"{PURPLE}\u2713{RESET}"
PROMPT = f"{PURPLE}\u276f{RESET} "

METER_WIDTH = 30

# How long the chosen key must stay down before the hotkey check calls
# itself passed: long enough to be a deliberate hold, short enough that
# nobody wonders whether it worked.
HOLD_PROVEN_S = 0.5


def meter_line(level: float, width: int = METER_WIDTH) -> str:
    """One line of level meter, 0.0-1.0 in, `width` cells out."""
    filled = round(max(0.0, min(1.0, level)) * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def transcript_line(text: str, width: int = 72) -> str:
    """The tail of the transcript that fits on one line."""
    text = " ".join(text.split())
    return text[-width:]


def keycap_line(key: str, held: bool) -> str:
    """The hotkey check's one line: the keycap, filled while held."""
    cap = f"[ {KEY_GLYPHS[key]} {KEY_LABELS[key]} ]"
    return f"{cap}  \u25c9 held" if held else f"{cap}  \u25cb up"


def rail(step: str) -> str:
    """The GUI's step rail as one breadcrumb line: travelled stages in
    ink, the active one purple, the rest dim."""
    active = next(i for i, (_, steps) in enumerate(CLI_STAGES)
                  if step in steps)
    sep = f" {DIM}\u203a{RESET} "
    return sep.join(
        f"{PURPLE}{BOLD}{label}{RESET}" if i == active else
        label if i < active else f"{DIM}{label}{RESET}"
        for i, (label, _) in enumerate(CLI_STAGES))


def key_cards(selected: int | None = None,
              width: int = 13) -> list[str]:
    """The key picker as a row of keycaps, four lines tall - the CLI's
    version of the GUI's chip cards. The number under each cap is its
    answer; the `selected` card is drawn purple."""
    rows: list[list[str]] = [[], [], [], []]
    for i, name in enumerate(KEY_MASKS):
        cells = ("\u256d" + "\u2500" * width + "\u256e",
                 "\u2502" + KEY_GLYPHS[name].center(width) + "\u2502",
                 "\u2502" + KEY_LABELS[name].center(width) + "\u2502",
                 "\u2570" + f" {i + 1} ".center(width, "\u2500")
                 + "\u256f")
        for row, cell in zip(rows, cells):
            row.append(f"{PURPLE}{BOLD}{cell}{RESET}" if i == selected
                       else cell)
    return ["  ".join(row) for row in rows]


def parse_choice(answer: str) -> str | None:
    """The key a picker answer names, or None for anything else."""
    keys = list(KEY_MASKS)
    answer = answer.strip()
    if answer in KEY_MASKS:
        return answer
    if answer.isdigit() and 1 <= int(answer) <= len(keys):
        return keys[int(answer) - 1]
    return None


def heading(step: str, key: str) -> str:
    title, body = (part.format(key=KEY_LABELS[key]) for part in COPY[step])
    wrapped = textwrap.fill(body, width=72)
    return (f"\n{rail(step)}\n\n"
            f"{BOLD}{title}{RESET}\n{DIM}{wrapped}{RESET}\n")


def raw_byte(fd: int, timeout: float) -> str:
    """One byte off `fd` within `timeout` seconds, or '' for none.
    The caller holds the terminal in cbreak mode."""
    ready, _, _ = select.select([fd], [], [], timeout)
    return os.read(fd, 1).decode(errors="replace") if ready else ""


def pressed_key(timeout: float) -> str:
    """One raw keypress within `timeout` seconds, or '' for none."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return raw_byte(fd, timeout)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def read_key(timeout: float) -> str:
    """A keypress with arrow keys named: 'left', 'right', or the char.

    The terminal stays in cbreak for the whole read - an arrow is three
    bytes, and restoring canonical mode mid-sequence would hand its tail
    to the line discipline."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = raw_byte(fd, timeout)
        if ch != "\x1b":
            return ch
        rest = raw_byte(fd, 0.05) + raw_byte(fd, 0.05)
        return {"[C": "right", "[D": "left"}.get(rest, "")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def wait_for_grant(flow: OnboardingFlow) -> None:
    """Sit on a permission step until its probe says granted."""
    open_pane(flow.step)
    spinner = "|/-\\"
    n = 0
    while not flow.poll():
        sys.stdout.write(f"{CLEAR_LINE}{DIM}waiting for the grant "
                         f"{spinner[n % 4]}{RESET}")
        sys.stdout.flush()
        n += 1
        time.sleep(0.25)
    sys.stdout.write(f"{CLEAR_LINE}{CHECK} granted.\n")


def mic_check(flow: OnboardingFlow, meter: MicMeter,
              ears: LiveTranscript | None = None) -> None:
    """The live meter with the words as they are heard beneath it,
    until the user says they saw it move."""
    with_words = ears is not None and ears.open()
    seen = "the meter moved" if not with_words else \
        "the meter moved and your words appeared"
    print(f"{DIM}speak, then press y if {seen} "
          f"(n for help, it keeps listening){RESET}")
    while flow.step == "mic_test":
        meter.open()
        sys.stdout.write(
            f"{CLEAR_LINE}{PURPLE}{meter_line(meter.read())}{RESET} ")
        if with_words:
            sys.stdout.write(f"\n{CLEAR_LINE}{DIM}"
                             f"{transcript_line(ears.read())}{RESET}\x1b[A\r")
        sys.stdout.flush()
        answer = pressed_key(0.05)
        if answer.lower() == "y":
            flow.confirm()
        elif answer.lower() == "n":
            flow.deny()
            if with_words:
                sys.stdout.write(f"\n{CLEAR_LINE}\x1b[A")
            sys.stdout.write(
                f"{CLEAR_LINE}{DIM}No movement? Pick an input in System "
                f"Settings > Sound, then speak again.{RESET}\n")
    heard = transcript_line(ears.read()) if with_words else ""
    if with_words:
        sys.stdout.write(f"\n{CLEAR_LINE}\x1b[A")
    said = f": \u201c{heard}\u201d" if heard else "."
    sys.stdout.write(f"{CLEAR_LINE}{CHECK} heard{said}\n")


def pick_key(flow: OnboardingFlow) -> None:
    """Arrows or a card's number move the purple highlight; Space or
    Enter take the highlighted card."""
    print(f"{DIM}\u2190 \u2192 or a card's number to choose, "
          f"Space or Enter to select{RESET}")
    keys = list(KEY_MASKS)
    selected = keys.index(flow.key) if flow.key in keys else 0
    for line in key_cards(selected):
        print(f"  {line}")
    while flow.step == "choose_key":
        answer = read_key(0.25)
        if answer in ("\r", "\n", " "):
            save_key(keys[selected])
            flow.choose(keys[selected])
            break
        if answer == "left":
            selected = (selected - 1) % len(keys)
        elif answer == "right":
            selected = (selected + 1) % len(keys)
        elif (key := parse_choice(answer)) is not None:
            selected = keys.index(key)
        else:
            continue
        sys.stdout.write(f"\x1b[{len(key_cards())}A")
        for line in key_cards(selected):
            sys.stdout.write(f"{CLEAR_LINE}  {line}\n")
        sys.stdout.flush()
    print(f"{CHECK} {KEY_LABELS[keys[selected]]} it is.")


def hotkey_check(flow: OnboardingFlow) -> None:
    """The keycap fills while the key is held; a real hold is the pass."""
    print(f"{DIM}hold it for half a second (or press s to skip){RESET}")
    held_since: float | None = None
    while flow.step == "hotkey_test":
        down = key_down(flow.key)
        now = time.monotonic()
        held_since = (held_since or now) if down else None
        cap = keycap_line(flow.key, down)
        sys.stdout.write(f"{CLEAR_LINE}{PURPLE if down else ''}{cap}{RESET} ")
        sys.stdout.flush()
        if down and now - held_since >= HOLD_PROVEN_S:
            flow.confirm()
            break
        if pressed_key(0.05).lower() == "s":
            flow.confirm()
            break
    sys.stdout.write(f"{CLEAR_LINE}{CHECK} that's the one.\n")


def cmux_check(flow: OnboardingFlow) -> None:
    """The one-time Homebrew install, run here so Gatekeeper's question
    is answered during onboarding rather than at first start; s skips."""
    from conductor import cmux_setup
    print(f"{DIM}Enter installs cmux with Homebrew and opens it once "
          f"(s skips for now){RESET}")
    while flow.step == "install_cmux":
        try:
            answer = input(f"{PROMPT}").strip().lower()
        except EOFError:
            flow.confirm()
            return
        if answer == "s":
            sys.stdout.write(f"{CLEAR_LINE}{DIM}skipped - the conductor "
                             f"installs it at first start{RESET}\n")
            flow.confirm()
            return
        ok, message = asyncio.run(cmux_setup.install())
        if not ok:
            print(message)
            print(f"{DIM}not installed yet - Enter to try again, s to "
                  f"skip{RESET}")
            continue
        # Its first launch is where macOS asks its "downloaded from the
        # internet" question, so it happens here, announced.
        print(f"{DIM}opening cmux once - when macOS asks about an app "
              f"downloaded from the internet, choose Open{RESET}")
        try:
            subprocess.run(["open", "-g", "-a", "cmux"],
                           capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass
        spinner = "|/-\\"
        n = 0
        while not cmux_setup.is_running() and n < 480:
            sys.stdout.write(f"{CLEAR_LINE}{DIM}waiting for cmux to open "
                             f"{spinner[n % 4]}{RESET}")
            sys.stdout.flush()
            n += 1
            time.sleep(0.25)
        cmux_setup.hide_app(for_s=2.0)
        sys.stdout.write(f"{CLEAR_LINE}{CHECK} cmux installed.\n")
        flow.confirm()
        return


def openai_key_check(flow: OnboardingFlow) -> None:
    """The hidden paste, saved to .env; Enter with nothing skips."""
    print(f"{DIM}the paste is hidden; Enter with nothing skips for now"
          f"{RESET}")
    while flow.step == "openai_key":
        key = getpass.getpass("OpenAI API key: ").strip()
        if not key:
            sys.stdout.write(f"{CLEAR_LINE}{DIM}skipped - add "
                             f"OPENAI_API_KEY=<your key> to .env next to "
                             f"the app, or the conductor asks again at "
                             f"startup{RESET}\n")
            flow.confirm()
            return
        save_openai_key(key)
        shadow = os.environ.get("OPENAI_API_KEY", "")
        if shadow and shadow != key:
            print(f"{DIM}heads up: this shell exports a different "
                  f"OPENAI_API_KEY, which overrides the saved one - "
                  f"unset it or start a fresh terminal{RESET}")
        sys.stdout.write(f"{CLEAR_LINE}{CHECK} saved to .env\n")
        flow.confirm()


def claude_check(flow: OnboardingFlow) -> None:
    """Claude Code's own browser sign-in, run right here; s skips."""
    if shutil.which("claude") is None:
        print(f"{DIM}Claude Code isn't installed yet - install it from "
              f"https://code.claude.com/docs/en/setup and sign in with: "
              f"claude auth login{RESET}")
        flow.confirm()
        return
    print(f"{DIM}Enter opens your browser to sign in "
          f"(s skips for now){RESET}")
    while flow.step == "claude_login":
        try:
            answer = input(f"{PROMPT}").strip().lower()
        except EOFError:
            flow.confirm()
            return
        if answer == "s":
            sys.stdout.write(f"{CLEAR_LINE}{DIM}skipped - sign in later "
                             f"with: claude auth login{RESET}\n")
            flow.confirm()
            return
        subprocess.run(["claude", "auth", "login"])
        if claude_logged_in():
            sys.stdout.write(f"{CLEAR_LINE}{CHECK} signed in.\n")
            flow.confirm()
            return
        print(f"{DIM}not signed in yet - Enter to try again, s to skip"
              f"{RESET}")


def main() -> int:
    meter = MicMeter()
    ears = LiveTranscript()
    flow = OnboardingFlow({
        "input_monitoring": input_monitoring_granted,
        "microphone": meter.granted,
        "accessibility": accessibility_granted,
        "screen_recording": screen_recording_granted,
        "install_cmux": cmux_installed,
        "openai_key": openai_key_saved,
        "claude_login": claude_logged_in,
    }, key=chosen_key(), steps=CLI_STEPS)
    try:
        while True:
            step = flow.step
            print(heading(step, flow.key))
            if step == "welcome":
                input(f"{PROMPT}press Enter to begin ")
                flow.confirm()
            elif step in OnboardingFlow.AUTO:
                wait_for_grant(flow)
            elif step == "mic_test":
                mic_check(flow, meter, ears)
            elif step == "choose_key":
                pick_key(flow)
            elif step == "hotkey_test":
                hotkey_check(flow)
            elif step == "install_cmux":
                cmux_check(flow)
            elif step == "openai_key":
                openai_key_check(flow)
            elif step == "claude_login":
                claude_check(flow)
            else:
                return 0
    except (KeyboardInterrupt, EOFError):
        print("\nonboarding left off; run ./conduct.sh --onboard-cli "
              "to pick it back up")
        return 130
    finally:
        ears.close()
        meter.close()


if __name__ == "__main__":
    sys.exit(main())
