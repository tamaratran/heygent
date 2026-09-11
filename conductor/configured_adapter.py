"""ConfiguredAdapter: a CLI described in providers.json instead of in code.

Every other adapter is a class someone measured a CLI for. Most CLIs
will never get one, and they do not need one to be useful: they run in
a terminal we host, read and type into. So a CLI can be named in
<home>/providers.json (~/.voice-conductor/providers.json by default):

    {"providers": {
      "aider": {
        "display": "Aider",
        "command": ["aider", "--no-auto-commits"],
        "brief": "--message",
        "prompt": "^>\\s*$",
        "prompt_marks": [">"],
        "busy": "Waiting for",
        "approval": "\\(Y\\)es/\\(N\\)o",
        "approve_keys": [["y"], ["Enter"]],
        "deny_keys": [["n"], ["Enter"]]
      }}}

Only `command` is required. With nothing else the CLI is started bare
in its checkout, the brief is typed in once the screen goes quiet, and
a turn is over when the output has been quiet for `settle_s`. Each
optional field buys back something that otherwise degrades:

    field          without it
    brief          the brief is typed in, as one line ("typed"); with it,
                   "argument" appends it to the command, "--flag" passes
                   it as that flag's value
    prompt         a turn ends on quiet alone, so a long silent think
                   reads as a finished turn; with it, quiet AND the
                   prompt back on screen
    prompt_marks   delivery is judged by the screen moving after Enter
                   and logged send.unverified when it does not; with it,
                   the input box is read, a message still in it is
                   pressed once more and then an error, and a person's
                   draft is set aside and restored
    box_at_bottom  (default true) the prompt line is the box only while
                   nothing but blanks, box_ends or chrome is under it -
                   a REPL's prompt scrolls into history; false for a TUI
                   that draws its own lines under a pinned box
    busy           text in the box while the CLI works reads as a draft
                   (matched against the bottom three lines only)
    approval       nothing is recognised as a permission prompt: the
                   question arrives as the text of a finished turn, and
                   the Boss answers it with send_to_task (matched in the
                   bottom `approval_lines`, default 2, lines)
    resume         no conversation to go back to: a resume starts the
                   command afresh in the same checkout
    submit         Enter
    chrome         status lines can land in the card body
    settle_s       5 s without a prompt pattern, 1.2 s with one

A line REPL (not a TUI) has one more limit: a person's draft set aside
while it works goes back as typeahead, which the REPL may not show at its
next prompt (measured live on a toy REPL; Claude Code, Codex and Cursor
restored drafts whole).

What never degrades, because it is the host's and not the CLI's: the
window and card, typing, focus, the process check, the sweep.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from pathlib import Path

from .screen_adapter import ScreenAdapter
from .task_types import PROVIDER_NAME as _NAME

PROVIDERS_FILE = "providers.json"
_NEVER = re.compile(r"$^")


class ConfigError(ValueError):
    """A providers.json entry that cannot be used, and why."""


class ConfiguredAdapter(ScreenAdapter):
    def __init__(self, name: str, spec: dict,
                 binary: str | None = None) -> None:
        command = spec.get("command")
        if isinstance(command, str):
            command = shlex.split(command)
        if not isinstance(command, list) or not command or \
                not all(isinstance(part, str) and part for part in command):
            raise ConfigError(f"{name}: command must be a non-empty list")
        self.name = name
        self.display = str(spec.get("display") or name)
        self.binary_name = os.path.basename(command[0])
        self.command = command
        super().__init__(binary or shutil.which(command[0]) or command[0])

        brief = spec.get("brief", "typed")
        if brief not in ("typed", "argument") and \
                not (isinstance(brief, str) and brief.startswith("-")):
            raise ConfigError(f"{name}: brief must be \"typed\", "
                              "\"argument\" or a flag like \"--message\"")
        self.brief = brief
        self.BRIEF_IN_ARGV = brief != "typed"
        # A line-oriented REPL submits at the first newline; a caller
        # whose CLI takes multi-line input says so.
        self.ONE_LINE = bool(spec.get("one_line", True))

        self.PROMPT = _pattern(name, spec, "prompt")
        self.BUSY = _pattern(name, spec, "busy", re.I)
        self.CHROME_LINES = _pattern(name, spec, "chrome") or _NEVER
        self._approval = _pattern(name, spec, "approval", re.I)
        try:
            self._approval_lines = max(1, int(spec.get("approval_lines", 2)))
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: approval_lines must be a number")
        self.PROMPT_MARKS = _strings(name, spec, "prompt_marks")
        self.BOX_ENDS = _strings(name, spec, "box_ends")
        self.PLACEHOLDERS = tuple(p.lower() for p in
                                  _strings(name, spec, "placeholders"))
        # A box someone described may still show a hint we were not told
        # about; the one-character probe tells hint from typing.
        self.SUGGESTS_IN_BOX = bool(self.PROMPT_MARKS)
        # A line REPL by default: its prompt line is only the box while
        # nothing is under it. A TUI with a footer lists the footer in
        # box_ends or chrome, or turns this off.
        self.BOX_AT_BOTTOM = bool(spec.get("box_at_bottom", True))
        self.SUBMIT_KEYS = _keys(name, spec, "submit", [["Enter"]])
        self._approve = _keys(name, spec, "approve_keys", [["y"], ["Enter"]])
        self._deny = _keys(name, spec, "deny_keys", [["Escape"]])
        resume = spec.get("resume")
        if isinstance(resume, str):
            resume = shlex.split(resume)
        self._resume = resume if isinstance(resume, list) and resume else None
        flags = spec.get("permission_flags") or {}
        self._permission_flags = {mode: list(extra)
                                  for mode, extra in flags.items()
                                  if isinstance(extra, list)}
        settle = spec.get("settle_s", 1.2 if self.PROMPT else 5.0)
        try:
            settle = float(settle)
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: settle_s must be a number of seconds")
        # Polls of the screen, WATCH_POLL_S (0.3 s) apart.
        self.SETTLE_POLLS = max(2, round(settle / 0.3))

    # -- launching ---------------------------------------------------------
    def launch_argv(self, prompt: str, permission_mode: str,
                    session_id: str | None = None) -> list[str]:
        argv = [self.binary, *self.command[1:],
                *self._permission_flags.get(permission_mode, [])]
        if self.brief == "argument":
            return argv + [prompt]
        if self.brief != "typed":
            return argv + [self.brief, prompt]
        return argv

    def resume_argv(self, session_id: str) -> list[str]:
        if self._resume is None:
            return [self.binary, *self.command[1:]]
        return [self.binary if part == self.command[0] else part
                for part in self._resume]

    # -- the screen -----------------------------------------------------------
    # How much of the bottom of the screen `busy` is matched against. A
    # REPL's "thinking..." stays in its history after the answer, and a
    # busy pattern matched anywhere kept a finished turn busy for ever
    # (measured live, 2026-09-11).
    BUSY_LINES = 3

    def busy(self, screen: str) -> bool:
        if self.BUSY is None:
            return False
        tail = [line for line in screen.splitlines() if line.strip()]
        tail = tail[-self.BUSY_LINES:]
        if self.PROMPT is not None:
            # Whatever the prompt is back under is over.
            last = max((i for i, line in enumerate(tail)
                        if self.is_prompt(line) or (
                            self.PROMPT_MARKS and i == len(tail) - 1
                            and line.strip().startswith(
                                tuple(m.strip() for m in self.PROMPT_MARKS)))),
                       default=-1)
            tail = tail[last + 1:]
        return bool(self.BUSY.search("\n".join(tail)))

    def is_prompt(self, line: str) -> bool:
        return self.PROMPT is not None and super().is_prompt(line)

    def prompt_ready(self, screen: str) -> bool:
        if self.busy(screen) or self.approval_prompt(screen) is not None:
            return False
        if self.PROMPT is None:
            # Nothing to recognise: a screen with something on it. The
            # watcher's settle (quiet for SETTLE_POLLS) does the rest.
            return bool(screen.strip())
        return super().prompt_ready(screen)

    def approval_prompt(self, screen: str) -> str | None:
        # Never the generic shapes: "allow" and "approve" are ordinary
        # words, and a false match here types approve_keys into a CLI
        # that asked nothing.
        # And only at the bottom of the screen: an answered question stays
        # in a REPL's history ("Run it? (y/n) y"), and matched anywhere
        # it read as still asking - the decision "did not clear" (measured
        # live, 2026-09-11). approval_lines says how far up a dialog's
        # question may sit above its last line.
        if self._approval is None:
            return None
        lines = self.lines(screen)
        start = max(0, len(lines) - self._approval_lines)
        for index in range(len(lines) - 1, start - 1, -1):
            if self._approval.search(lines[index]):
                return " | ".join(lines[max(0, index - 3):index + 1])[:300]
        return None

    def approve_keys(self, screen: str) -> list[list[str]]:
        return [list(keys) for keys in self._approve]

    def deny_keys(self, screen: str) -> list[list[str]]:
        return [list(keys) for keys in self._deny]

    def process_needle(self, session_id: str | None) -> str | None:
        return None             # screen sessions have invented ids


def _pattern(name: str, spec: dict, key: str, flags: int = 0):
    raw = spec.get(key)
    if not raw:
        return None
    try:
        return re.compile(raw, flags | re.M)
    except (re.error, TypeError) as exc:
        raise ConfigError(f"{name}: {key} is not a valid pattern ({exc})")


def _strings(name: str, spec: dict, key: str) -> tuple[str, ...]:
    raw = spec.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(x, str) and x
                                            for x in raw):
        raise ConfigError(f"{name}: {key} must be a list of strings")
    return tuple(raw)


def _keys(name: str, spec: dict, key: str, default: list[list[str]]):
    raw = spec.get(key, default)
    if not isinstance(raw, list) or not raw or not all(
            isinstance(seq, list) and seq and all(isinstance(k, str) and k
                                                  for k in seq)
            for seq in raw):
        raise ConfigError(f"{name}: {key} must be a list of key lists, "
                          "like [[\"Enter\"]]")
    return tuple(tuple(seq) for seq in raw)


# -- the file ----------------------------------------------------------------

# name -> spec, as last loaded. adapter_for looks here after the built-in
# adapters, so a configured name that collides with one does not replace it.
CONFIGURED: dict[str, dict] = {}


def providers_path(home: str | Path | None = None) -> Path:
    base = Path(home).expanduser() if home else \
        Path.home() / ".voice-conductor"
    return base / PROVIDERS_FILE


def load(home: str | Path | None = None) -> tuple[dict[str, dict], list[str]]:
    """The usable entries of providers.json, and what was wrong with the
    rest. A broken entry is skipped and reported, never fatal: one typo
    must not keep the conductor from starting. Loading also makes the
    entries known to adapter_for."""
    path = providers_path(home)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}, []
    except (OSError, ValueError) as exc:
        return {}, [f"{path}: {exc}"]
    entries = data.get("providers", data) if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}, [f"{path}: expected {{\"providers\": {{name: {{...}}}}}}"]
    good, problems = {}, []
    for name, spec in entries.items():
        if not isinstance(name, str) or not _NAME.match(name):
            problems.append(f"{name!r}: a provider name is lowercase letters, "
                            "digits, dots, dashes or underscores")
            continue
        if not isinstance(spec, dict):
            problems.append(f"{name}: expected an object")
            continue
        try:
            ConfiguredAdapter(name, spec)
        except ConfigError as exc:
            problems.append(str(exc))
            continue
        good[name] = spec
    CONFIGURED.clear()
    CONFIGURED.update(good)
    return good, problems


def binaries(spec: dict) -> tuple[str, ...]:
    command = spec.get("command")
    if isinstance(command, str):
        command = shlex.split(command)
    return (command[0],) if command else ()
