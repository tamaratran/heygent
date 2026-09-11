"""ScreenAdapter: a CLI known only by its terminal - and Gemini, built on it.

Phase 2 of docs/any-cli.md. Claude Code writes a transcript we can tail;
most CLIs do not, or not usefully. But every one of them runs in a
terminal cmux lets us read, so the screen is the universal source:

    turn end   the CLI's input prompt is back after activity and the
               screen has not changed for SETTLE_POLLS polls
    text       what appeared on screen since the words went in, minus
               the CLI's own chrome
    approval   a question shaped like a choice (the runtime's
               _PROMPT_SHAPES, or the adapter's own markers)
    alive      the host says so; the process table confirms

Lower fidelity than a transcript - no tool names, no structured
results - and fully universal. A real adapter starts as this plus a
prompt regex, and graduates to a transcript parser when one is
measured.

Gemini CLI (0.35.2 here) keeps ~/.gemini/tmp/<project>/logs.json (the
user's prompts, not the turns) and chats/ (checkpoints on request),
neither of which says when a turn ended - so it is a ScreenAdapter with
Gemini's dialogs, flags and prompt markers.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .agent_events import AgentEvent
from .cli_adapter import CliAdapter


class ScreenAdapter(CliAdapter):
    """Turn ends and text read off the screen. Subclasses say what the
    CLI's prompt looks like (PROMPT) and which lines are chrome."""

    name = "screen"
    display = "terminal CLI"
    # The input prompt, back on screen - matched against a line with its
    # furniture stripped. Subclasses set a tighter one.
    PROMPT = re.compile(r"^[>❯]\s*$")
    # How many unchanged polls (WATCH_POLL_S apart) a ready prompt must
    # survive before the turn is taken as over: the CLI can redraw its
    # prompt between tool calls.
    SETTLE_POLLS = 4

    # Box-drawing furniture a TUI puts around its content. Stripped
    # from both ends of every line before anything is judged, so a
    # prompt inside a box ("│ > ") is still a prompt.
    _BOX = "│┃║╭╰╮╯─═┌└┐┘ \t"

    @classmethod
    def clean(cls, line: str) -> str:
        return line.strip(cls._BOX)

    # Whole lines that are the CLI's own status, never the answer: a
    # footer, a spinner, a hint. Subclasses set a pattern; measured
    # live, Cursor's "Auto · 8.3%" landed in the card body.
    CHROME_LINES = re.compile(r"$^")

    def lines(self, screen: str) -> list[str]:
        """The screen's content lines, furniture removed, blanks and the
        CLI's own status lines dropped."""
        out = []
        for raw in screen.splitlines():
            line = self.clean(raw)
            if line and not self.CHROME_LINES.match(line):
                out.append(line)
        return out

    def box_ends(self, stripped: str) -> bool:
        # The CLI's own status lines sit under its box, indented like a
        # wrapped line; they close it rather than join the message.
        return bool(self.CHROME_LINES.match(stripped)) or \
            super().box_ends(stripped)

    def is_prompt(self, line: str) -> bool:
        return bool(self.PROMPT.search(self.clean(line)))

    def prompt_ready(self, screen: str) -> bool:
        return any(self.is_prompt(ln) for ln in screen.splitlines())

    # What a pane looks like when it is waiting on the user: the shape
    # of a choice rather than any one CLI's wording. Subclasses add
    # their own markers.
    APPROVAL_SHAPES = ("do you want", "would you like", "1. yes", "(y/n)",
                       "allow", "approve", "permission to")

    def approval_prompt(self, screen: str) -> str | None:
        if self.startup_dialog(screen) is not None:
            return None
        low = screen.lower()
        if not any(shape in low for shape in self.APPROVAL_SHAPES):
            return None
        lines = self.lines(screen)
        for index, line in enumerate(lines):
            if any(shape in line.lower() for shape in self.APPROVAL_SHAPES):
                return " | ".join(lines[max(0, index - 3):index]) or "permission request"
        return "permission request"

    def transcript_dir(self, working_directory: str):
        return None                 # the screen is the transcript

    # -- the screen as a transcript ---------------------------------------
    def screen_events(self, screen: str, state: dict) -> list[AgentEvent]:
        """One poll of the screen -> zero or more AgentEvents. `state`
        is the session's, kept between polls."""
        events: list[AgentEvent] = []
        body = self.lines(screen)
        last = state.get("screen_last")
        if body != last:
            state["screen_last"] = body
            state["screen_stable"] = 0
            if last is not None:
                # Something moved. The newest non-prompt line is the
                # activity; the turn is running.
                fresh = [ln for ln in body if ln not in last]
                if fresh and not state.get("screen_busy"):
                    state["screen_busy"] = True
                    state["screen_since"] = list(last)
                said = [ln for ln in fresh if not self.is_prompt(ln)]
                if said:
                    events.append(AgentEvent(type="progress",
                                             summary=said[-1][:300]))
            return events
        state["screen_stable"] = state.get("screen_stable", 0) + 1
        if state.get("screen_busy") and self.prompt_ready(screen) \
                and state["screen_stable"] >= self.SETTLE_POLLS:
            # Quiet, prompt back: the turn is over. Its text is what
            # appeared since it started, without the prompt itself.
            before = state.get("screen_since") or []
            said = [ln for ln in body
                    if ln not in before and not self.is_prompt(ln)]
            state["screen_busy"] = False
            state.pop("screen_since", None)
            events.append(AgentEvent(type="completed",
                                     summary=" ".join(said)[-600:]))
        return events


class GeminiAdapter(ScreenAdapter):
    """Gemini CLI. Measured on 0.35.2: the trust dialog ("● 1. Trust
    folder"), the auth dialog ("How would you like to authenticate"),
    and the flags below. The ready-prompt marker is from its source
    and its docs, not yet from a signed-in screen here: verify it once
    a session is authenticated (see docs/any-cli.md)."""

    name = "gemini"
    display = "Gemini CLI"
    binary_name = "gemini"
    PROMPT = re.compile(r"(Type your message|@path/to/file|^>\s*$)")
    # The box "│ > text │" inside its frame, closed by "╰───╯". Like the
    # ready prompt, from Gemini's source rather than a signed-in screen.
    PROMPT_MARKS = (">",)
    BOX_ENDS = ("╰", "─")
    PLACEHOLDERS = ("type your message",)
    SUGGESTS_IN_BOX = True
    BUSY = re.compile(r"esc to cancel", re.I)
    _APPROVAL = ("allow execution", "apply this change", "allow once",
                 "yes, allow", "do you want to proceed")

    # Our "auto": edits proceed, commands ask - the asking is what the
    # card and the Boss see as an approval. "bypassPermissions" maps to
    # yolo; anything else asks for everything.
    _MODES = {"auto": "auto_edit", "acceptEdits": "auto_edit",
              "bypassPermissions": "yolo", "default": "default"}

    def launch_argv(self, prompt: str, permission_mode: str,
                    session_id: str | None = None) -> list[str]:
        mode = self._MODES.get(permission_mode, "auto_edit")
        return [self.binary, "--approval-mode", mode,
                "--prompt-interactive", prompt]

    def resume_argv(self, session_id: str,
                    permission_mode: str | None = None) -> list[str]:
        # Gemini resumes by index or "latest", not by id: the newest
        # session in this checkout is the one we started there.
        mode = ["--approval-mode", self._MODES.get(permission_mode, "auto_edit")] \
            if permission_mode else []
        return [self.binary, *mode, "--resume", "latest"]

    def startup_dialog(self, screen: str) -> str | None:
        low = screen.lower()
        if "trust folder" in low or "trust parent folder" in low:
            return "trust"
        if "how would you like to authenticate" in low:
            return "auth"
        return None

    def approval_prompt(self, screen: str) -> str | None:
        low = screen.lower()
        if not any(mark in low for mark in self._APPROVAL):
            return None
        lines = self.lines(screen)
        for index, line in enumerate(lines):
            if any(mark in line.lower() for mark in self._APPROVAL):
                return " | ".join(lines[max(0, index - 3):index]) or "permission request"
        return "permission request"

    def process_needle(self, session_id: str | None) -> str | None:
        return None                 # not on its command line; the checkout finds it


class CursorAdapter(ScreenAdapter):
    """Cursor's terminal agent (`cursor-agent`, also installed as
    `agent`). Measured on 2026.08.25-3e8eec8, installed here on
    2026-08-30: `--trust` skips the workspace-trust prompt, `--force`
    (`--yolo`) runs commands unasked, `--resume <chatId>` resumes, and
    a CLI that is not signed in shows "Press any key to log in..." -
    which only the user can do (`cursor-agent login` opens a browser).
    It keeps per-project state under ~/.cursor/projects/<cwd with
    slashes as dashes>. What a signed-in turn looks like on screen, and
    whether that state holds a readable transcript, are still to be
    measured after the user signs in; until then it is a ScreenAdapter
    with the generic prompt and approval shapes."""

    name = "cursor"
    display = "Cursor"
    binary_name = "cursor-agent"
    # Measured signed in (2026-08-30): the composer is an arrow prompt,
    # "→ Plan, search, build anything" when empty; a command approval
    # reads "Run this command? / Not in allowlist: touch / → Run (once)
    # (y) / Add Shell(touch) to allowlist? (tab) / Run Everything
    # (shift+tab) / Skip & tell the agent what to do instead (esc or n)",
    # with "Waiting for approval..." in the transcript above it. The
    # arrow marks the selected option there too, so a ready prompt is an
    # arrow line on a screen that is not asking.
    PROMPT = re.compile(r"^→\s")
    # The composer as a box: "→ " and the text, the footer under it (its
    # CHROME_LINES close it). Empty, it shows a hint: "Plan, search, build
    # anything", and "Add a follow-up" after a turn (both measured).
    PROMPT_MARKS = ("→",)
    PLACEHOLDERS = ("plan, search, build anything", "add a follow-up")
    SUGGESTS_IN_BOX = True
    # The braille spinner line ("⠞ Reading 56 tokens"), measured in a
    # card body, is up only while a turn runs.
    BUSY = re.compile(r"(?m)^\s*[⠀-⣿]\s")
    _APPROVAL = ("run this command?", "not in allowlist", "waiting for approval")
    # Measured in a card body: the model/context footer ("Auto · 8.3%"),
    # the cwd line, the tips, the braille spinner ("⠞ Reading 56 tokens").
    CHROME_LINES = re.compile(r"^(Auto\b.*|~/.*|/Users/.*|Tip: .*|Cursor Agent|"
                              r"v\d{4}\.\d{2}\.\d{2}.*|[⠀-⣿].*)$")

    def __init__(self, binary: str | None = None) -> None:
        super().__init__(binary or shutil.which("cursor-agent")
                         or shutil.which("agent") or "cursor-agent")

    def prompt_ready(self, screen: str) -> bool:
        if self.approval_prompt(screen) is not None:
            return False
        return super().prompt_ready(screen)

    def approval_prompt(self, screen: str) -> str | None:
        if self.startup_dialog(screen) is not None:
            return None
        low = screen.lower()
        if not any(mark in low for mark in self._APPROVAL):
            return None
        lines = self.lines(screen)
        # The command is the "$ ..." line nearest above the question.
        asked = next((i for i, ln in enumerate(lines)
                      if "run this command" in ln.lower()), len(lines))
        for line in reversed(lines[:asked]):
            if line.startswith("$"):
                return line.lstrip("$ ").split(" Waiting")[0].strip()[:200]
        return "permission request"

    def launch_argv(self, prompt: str, permission_mode: str,
                    session_id: str | None = None) -> list[str]:
        # --trust: a checkout we made is not a stranger's; the trust
        # prompt would only hold the worker at its boot screen.
        argv = [self.binary, "--trust"]
        if permission_mode == "bypassPermissions":
            argv.append("--force")
        return argv + [prompt]

    def resume_argv(self, session_id: str,
                    permission_mode: str | None = None) -> list[str]:
        force = ["--force"] if permission_mode == "bypassPermissions" else []
        return [self.binary, "--trust", *force, "--resume", session_id]

    def startup_dialog(self, screen: str) -> str | None:
        low = screen.lower()
        if "press any key to log in" in low or "cursor-agent login" in low:
            return "auth"
        if "trust" in low and ("workspace" in low or "folder" in low
                               or "directory" in low):
            return "trust"
        return None


class _BusyAwareScreenAdapter(ScreenAdapter):
    """A ScreenAdapter whose prompt is not "back" while the CLI says it is
    working, is on a startup dialog, or is asking. ScreenAdapter's settle
    ignores busy, which is how Cursor reported a turn finished mid-turn
    (live, 2026-09-11)."""

    # Only these words are an approval: the generic shapes ("allow",
    # "approve") are ordinary words, and a false match types approve_keys
    # into a CLI that asked nothing.
    _APPROVAL: tuple[str, ...] = ()

    def prompt_ready(self, screen: str) -> bool:
        if self.startup_dialog(screen) is not None or self.busy(screen) \
                or self.approval_prompt(screen) is not None:
            return False
        return super().prompt_ready(screen)

    def approval_prompt(self, screen: str) -> str | None:
        if not self._APPROVAL or self.startup_dialog(screen) is not None:
            return None
        lines = self.lines(screen)
        for index, line in enumerate(lines):
            if any(mark in line.lower() for mark in self._APPROVAL):
                return " | ".join(lines[max(0, index - 3):index]) or "permission request"
        return None

    def process_needle(self, session_id: str | None) -> str | None:
        return None                 # screen sessions have invented ids


class DevinAdapter(_BusyAwareScreenAdapter):
    """Devin's terminal agent (`devin`, 3000.6.7 here, installed by Devin
    Desktop). Measured 2026-09-10: its --help, and the log-in screen a
    signed-out CLI shows ("Welcome to Devin CLI! / How would you like to
    log in?"). It is not signed in here, so a working screen has not been
    seen: the placeholder, busy and approval words below are from the
    binary's strings. It keeps sessions in a database, not a transcript
    we can tail, so it is read off the screen."""

    name = "devin"
    display = "Devin"
    binary_name = "devin"
    # --permission-mode: "auto" approves read-only tools, "accept-edits"
    # also workspace edits, "smart" what a fast model judges safe,
    # "dangerous" every tool.
    _MODES = {"auto": "smart", "acceptEdits": "accept-edits",
              "default": "auto", "bypassPermissions": "dangerous"}
    # The empty box's hint, or a bare prompt character.
    PROMPT = re.compile(r"(Ask Devin to |^[>❯❭›]\s*$)")
    PLACEHOLDERS = ("ask devin to",)
    # The box's hint while a turn runs, and the interrupt hint.
    BUSY = re.compile(r"(Guide Devin while it works|esc (again|twice) to interrupt)",
                      re.I)
    _APPROVAL = ("yes, allow once",)

    def launch_argv(self, prompt: str, permission_mode: str,
                    session_id: str | None = None) -> list[str]:
        # The "--" is load-bearing: `devin [PATH]... [-- <PROMPT>...]`, so
        # a brief without it is a path, and Devin Desktop opens on it.
        return [self.binary, "--permission-mode",
                self._MODES.get(permission_mode, "smart"), "--", prompt]

    def resume_argv(self, session_id: str,
                    permission_mode: str | None = None) -> list[str]:
        mode = ["--permission-mode", self._MODES.get(permission_mode, "smart")] \
            if permission_mode else []
        if session_id and not session_id.startswith("scr_"):
            return [self.binary, *mode, "--resume", session_id]
        # A screen session's id is ours, not Devin's. Its sessions are
        # listed per directory (`devin list`), and each worker has its own
        # checkout, so the most recent one there is the one we started.
        return [self.binary, *mode, "--continue"]

    def startup_dialog(self, screen: str) -> str | None:
        low = screen.lower()
        # Not "Welcome to Devin CLI!" alone: a banner can outlive the
        # log-in, and a dialog that never clears holds the worker for good.
        if "how would you like to log in" in low:
            return "auth"
        # "Do you trust the authors of <dir>?" / "Yes, trust ..." (strings).
        if "do you trust the authors of" in low:
            return "trust"
        return None


class DroidAdapter(_BusyAwareScreenAdapter):
    """Factory's Droid (`droid`, 0.73.0 here). Measured 2026-09-10: its
    --help, and the screen a signed-out CLI shows ("Please login with your
    Factory account to continue." over "> Login / Exit"). It is not signed
    in here; the busy words are from the binary's strings.

    Interactive droid has no permission flag - `--skip-permissions-unsafe`
    and `--auto` belong to `droid exec`. Its autonomy level is a setting,
    and `--settings <path>` merges a settings file for this process only.
    The file's shape is settings.json's, from the binary: the level sits in
    sessionDefaultSettings, and a "general" wrapper is refused. "high" is
    "allow all commands", the most the interactive CLI offers."""

    name = "droid"
    display = "Droid"
    binary_name = "droid"
    _AUTONOMY = {"auto": "medium", "acceptEdits": "low",
                 "bypassPermissions": "high"}
    # Where the overlay files are written, one per level.
    SETTINGS_DIR = Path.home() / ".voice-conductor" / "cli-settings"
    BUSY = re.compile(r"press esc to stop", re.I)

    def settings_overlay(self, permission_mode: str | None) -> list[str]:
        """["--settings", <file>] for our mode's autonomy level, or [] for
        a mode that leaves droid at the user's own default."""
        level = self._AUTONOMY.get(permission_mode or "")
        if level is None:
            return []
        path = Path(self.SETTINGS_DIR) / f"droid-autonomy-{level}.json"
        body = json.dumps({"sessionDefaultSettings": {"autonomyLevel": level}})
        try:
            if not path.is_file() or path.read_text() != body:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
        except OSError:
            return []               # droid refuses a missing file; start it plain
        return ["--settings", str(path)]

    def launch_argv(self, prompt: str, permission_mode: str,
                    session_id: str | None = None) -> list[str]:
        return [self.binary, *self.settings_overlay(permission_mode), prompt]

    def resume_argv(self, session_id: str,
                    permission_mode: str | None = None) -> list[str]:
        argv = [self.binary, *self.settings_overlay(permission_mode)]
        if session_id and not session_id.startswith("scr_"):
            return argv + ["--resume", session_id]
        # A bare --resume takes droid's last modified session, which need
        # not be this checkout's; starting afresh is the safe miss.
        return argv

    def startup_dialog(self, screen: str) -> str | None:
        if "please login with your factory account" in screen.lower():
            return "auth"
        return None


ADAPTERS = {"gemini": GeminiAdapter, "cursor": CursorAdapter,
            "devin": DevinAdapter, "droid": DroidAdapter,
            "screen": ScreenAdapter}
