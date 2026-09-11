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

import re
import shutil

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

    def resume_argv(self, session_id: str) -> list[str]:
        # Gemini resumes by index or "latest", not by id: the newest
        # session in this checkout is the one we started there.
        return [self.binary, "--resume", "latest"]

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

    def resume_argv(self, session_id: str) -> list[str]:
        return [self.binary, "--trust", "--resume", session_id]

    def startup_dialog(self, screen: str) -> str | None:
        low = screen.lower()
        if "press any key to log in" in low or "cursor-agent login" in low:
            return "auth"
        if "trust" in low and ("workspace" in low or "folder" in low
                               or "directory" in low):
            return "trust"
        return None


ADAPTERS = {"gemini": GeminiAdapter, "cursor": CursorAdapter,
            "screen": ScreenAdapter}
