"""CliAdapter: what the PTY runtime is hosting, one adapter per CLI.

cmux holds any terminal. The runtime that hosts a worker in one -
create a workspace, type into it, read its screen, keep it alive, close
it - has nothing of Claude's in it. What it had was Claude Code's
answers to "what is this CLI doing", inline, in twenty-nine places:
the launch flags, the shape of a ready input box, where the transcript
is written and how to read it, what a permission prompt looks like,
which dialogs precede the first prompt. Those answers are one object
now, and the runtime asks it. Another CLI is another object.

    PtyHost (tmux | cmux)  +  CliAdapter  =  the runtime we had

The Claude Code adapter is the code that was in tmux_runtime.py, moved
here unchanged; tmux_runtime re-exports the names it used to define so
nothing that imported them notices. docs/any-cli.md says why.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .agent_events import AgentEvent, SUMMARY_CEILING, keep_end


class CliAdapter:
    """The questions the runtime asks about the CLI it hosts. This base
    answers as a CLI with no transcript would: nothing to read, nothing
    to resume by id, no dialogs. Real adapters override what they know."""

    name = "generic"                # what capabilities and tasks call it
    display = "CLI"
    binary_name = ""

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or (shutil.which(self.binary_name)
                                 if self.binary_name else None) \
            or self.binary_name

    # -- launching ---------------------------------------------------------
    def launch_argv(self, prompt: str, permission_mode: str,
                    session_id: str | None = None) -> list[str]:
        """The command line for a fresh worker in its checkout."""
        return [self.binary, prompt]

    def resume_argv(self, session_id: str) -> list[str]:
        return [self.binary]

    # -- the screen -----------------------------------------------------------
    def prompt_ready(self, screen: str) -> bool:
        """The input box is accepting text."""
        return True

    def approval_prompt(self, screen: str) -> str | None:
        """If the screen sits on a permission prompt, what it asks."""
        return None

    def startup_dialog(self, screen: str) -> str | None:
        """A dialog shown before the first prompt that must be answered:
        one of "trust", "resume_picker", "chrome" - or None."""
        return None

    # -- the input box ------------------------------------------------------
    # How delivery is checked. The runtime used to answer these with
    # Claude Code's characters for every CLI: a line starting with "❯" or
    # ">" was the box, "esc to interrupt" was busy, Enter submitted. Codex
    # draws "›", Cursor "→", Gemini "│ >" - so for them the box was never
    # found, a follow-up still sitting in it read as submitted, and a
    # person's draft was never seen. A CLI that says nothing here has a box
    # nobody can read: delivery is then judged by the screen moving, and
    # said to be unverified (see TmuxClaudeRuntime.send).
    PROMPT_MARKS: tuple[str, ...] = ()
    # What closes the box from below, as the start of a stripped line.
    BOX_ENDS: tuple[str, ...] = ()
    # Hint text the CLI draws in an EMPTY box (lowercase prefixes).
    PLACEHOLDERS: tuple[str, ...] = ()
    # What is on screen only while a turn is running.
    BUSY: re.Pattern | None = None
    # The keys that submit what was typed.
    SUBMIT_KEYS: tuple[tuple[str, ...], ...] = (("Enter",),)
    # Draws its own suggestion in the box, indistinguishable from typing
    # but for behaviour: the runtime probes one character to tell.
    SUGGESTS_IN_BOX = False
    # The brief goes on the command line (launch_argv). False: the CLI is
    # started bare and the brief is typed in once it is listening.
    BRIEF_IN_ARGV = True
    # Newlines in typed text submit early in a line-oriented REPL, so a
    # CLI that says so gets every message as one line.
    ONE_LINE = False
    # The box is the bottom of the screen: only blank lines and lines
    # box_ends accepts may follow it. True for a line REPL, whose prompt
    # scrolls up into history with the text that was submitted still on
    # it - read as the box, a message already taken looks unsent and its
    # Enter is pressed again (measured live on a REPL, 2026-09-11). A TUI
    # pins its box and draws whatever it likes under it, so False there.
    BOX_AT_BOTTOM = False

    def input_box(self, screen: str) -> str | None:
        """What is in the input box, or None when this adapter cannot find
        one on this screen (or knows no box at all)."""
        if not self.PROMPT_MARKS:
            return None
        return read_input_box(screen, self.PROMPT_MARKS, self.box_ends,
                              at_bottom=self.BOX_AT_BOTTOM)

    def box_ends(self, stripped: str) -> bool:
        """Does this line close the box from below?"""
        return bool(self.BOX_ENDS) and stripped.startswith(self.BOX_ENDS)

    @property
    def reads_input_box(self) -> bool:
        return bool(self.PROMPT_MARKS)

    def draft(self, screen: str) -> str:
        """What a PERSON has half-written in the box, or "": the box, less
        the CLI's own hint text."""
        box = self.input_box(screen)
        if not box or box.lower().startswith(self.PLACEHOLDERS):
            return ""
        return box

    def busy(self, screen: str) -> bool:
        """A turn is running (text in the box would be queued, not idle)."""
        return bool(self.BUSY and self.BUSY.search(screen))

    def submit_keys(self) -> list[list[str]]:
        return [list(keys) for keys in self.SUBMIT_KEYS]

    def prepare_text(self, message: str) -> str:
        return " ".join(message.split()) if self.ONE_LINE else message

    # -- answering a permission prompt ---------------------------------------
    def approve_keys(self, screen: str) -> list[list[str]]:
        """The send-keys sequences that accept the prompt on screen.
        Claude Code's, and measured to work on Codex 0.151 too: option
        1 is the "yes, this once" in both."""
        return [["1"], ["Enter"]]

    def deny_keys(self, screen: str) -> list[list[str]]:
        """The send-keys sequences that refuse it."""
        return [["Escape"]]

    # -- the transcript ----------------------------------------------------------
    def transcript_dir(self, working_directory: str) -> Path | None:
        """Where this CLI writes its own session log for that checkout,
        or None if it writes none (the screen is then the only source)."""
        return None

    def transcript_for(self, working_directory: str,
                       session_id: str) -> Path | None:
        directory = self.transcript_dir(working_directory)
        return directory / f"{session_id}.jsonl" if directory else None

    def transcripts(self, working_directory: str) -> list[Path]:
        """Every session log this CLI has for that checkout - what
        discovery chooses the newest of. Flat directories by default; a
        CLI that files its logs another way overrides this."""
        directory = self.transcript_dir(working_directory)
        if directory is None or not directory.is_dir():
            return []
        return list(directory.glob("*.jsonl"))

    def session_id_of(self, transcript: Path) -> str:
        """The session id a transcript file names."""
        return transcript.stem

    def normalize(self, entry: dict, state: dict) -> list[AgentEvent]:
        """One line of the CLI's own log -> zero or more AgentEvents."""
        return []

    def finished_turn(self, transcript: Path) -> str | None:
        """The final text of a turn that had already ended by the end of
        the transcript, or None while a turn is open. A CLI with no
        transcript has no answer."""
        return None

    def screen_events(self, screen: str, state: dict) -> list[AgentEvent]:
        """One poll of the screen -> zero or more AgentEvents, for a CLI
        with no transcript to tail. The base knows nothing; see
        ScreenAdapter."""
        return []

    # -- identity -----------------------------------------------------------------
    def process_needle(self, session_id: str | None) -> str | None:
        """A string on the running process's command line that names
        this session, for the process table's second opinion."""
        return session_id or None


# Vertical frame furniture a TUI draws at the edges of its box ("│ > hi │").
_FRAME = "│┃║"


def read_input_box(screen: str, marks: tuple[str, ...],
                   ends=lambda stripped: False,
                   at_bottom: bool = False) -> str | None:
    """Everything in the input box, joined across the lines it wraps onto.

    Or None when no prompt line is on screen. The box starts on the last
    line whose content begins with one of `marks`; a message longer than
    the pane is wide carries on underneath, indented, until a blank line
    or a line `ends` says closes the box (a rule, a status line). With
    at_bottom, a box with anything else under it is history, not a box.

    Reading only that first line is what lost a message on 2026-09-10.
    The Boss sent task_84d3c789 a 239-character follow-up at 06:55:10;
    the Enter did not take; and the delivery check looked for the
    message's LAST forty characters on the prompt line - where a wrapped
    message's ending never is. It reported "submitted" at once, the Boss
    told the user it was sent, and the words sat in the worker's box for
    four minutes until the user opened the window and pressed Enter.
    """
    lines = [line.strip(_FRAME).rstrip() for line in screen.splitlines()]
    start = None
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if stripped and stripped.startswith(marks):
            start = index
            break
    if start is None:
        return None
    first = lines[start].strip()
    mark = next(m for m in marks if first.startswith(m))
    parts = [first[len(mark):]]
    below = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped or not line.startswith("  ") or ends(stripped):
            below = index
            break
        parts.append(stripped)
    if at_bottom and any(line.strip() and not ends(line.strip())
                         for line in lines[below:]):
        return None
    return " ".join(part.replace("\xa0", " ").strip()
                    for part in parts).strip()


# -- Claude Code -----------------------------------------------------------------

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Markers of Claude Code's TUI permission prompt. The pane owned by THIS
# runtime is the authoritative prompt state; decisions are answered through
# this exact PTY, bound to session + approval ids - never by finding "some
# terminal" and typing y.
_PROMPT_MARKERS = ("do you want to proceed", "❯ 1. yes")

# The footer claude draws once its input box is accepting text.
PROMPT_READY = re.compile(r"(shift\+tab to cycle|for shortcuts|⏵⏵)")


def detect_approval_prompt(pane_text: str) -> str | None:
    """If the pane is sitting on a permission prompt, return a description
    of what is being asked (the lines above the question); else None."""
    lower = pane_text.lower()
    if not any(marker in lower for marker in _PROMPT_MARKERS):
        return None
    lines = [line.rstrip() for line in pane_text.splitlines()]
    cut = len(lines)
    for index, line in enumerate(lines):
        if "do you want" in line.lower() or "❯ 1." in line.lower():
            cut = index
            break
    context = [line.strip() for line in lines[:cut] if line.strip()]
    return " | ".join(context[-3:]) if context else "permission request"


def munge_project_dir(cwd: str | Path) -> str:
    """The directory name Claude Code uses for a working directory:
    every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9-]", "-", str(Path(cwd).resolve()))


def normalize_entry(entry: dict, state: dict) -> list[AgentEvent]:
    """One session-JSONL entry -> zero or more AgentEvents.

    state carries turn accumulation between calls:
        {"turn_text": [...]}
    """
    if entry.get("isSidechain"):
        # A Task-tool subagent's traffic, written into the same session
        # file as the worker's own. Its final message carries end_turn,
        # which is the subagent's turn ending - not the worker's. Reading
        # it as the worker's finish reported the task answered while the
        # worker was still going.
        return []
    events: list[AgentEvent] = []
    kind = entry.get("type")
    message = entry.get("message") or {}
    content = message.get("content")
    if _resume_filler(entry, kind, message, content):
        return []

    if kind == "user":
        if state.pop("turn_ending", False):
            # The last turn ended on a thought and no text followed; the
            # next message is here, so it is over.
            events.append(_turn_end(state))
        text = content if isinstance(content, str) else " ".join(
            block.get("text", "") for block in (content or [])
            if isinstance(block, dict) and block.get("type") == "text")
        text = " ".join(str(text).split())
        if text:
            # Direct terminal input and conductor sends both surface here:
            # the Manager keeps tracking the same subagent either way.
            events.append(AgentEvent(type="progress",
                                     summary=f"> {text[:200]}",
                                     detail={"source": "user_message"}))
    elif kind == "assistant":
        said = False                  # text or a tool call in THIS entry
        for block in (content or []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                said = True
                text = block["text"].strip()
                state.setdefault("turn_text", []).append(text)
                events.append(AgentEvent(type="progress",
                                         summary=text[:300],
                                         text=text[:SUMMARY_CEILING]))
            elif block.get("type") == "tool_use":
                said = True
                name = block.get("name", "tool")
                args = block.get("input") or {}
                gist = next((str(args[k])[:70] for k in
                             ("file_path", "command", "pattern", "path")
                             if k in args), "")
                events.append(AgentEvent(
                    type="progress",
                    summary=f"{name}({gist})" if gist else name,
                    detail={"tool": name}))
        if message.get("stop_reason") in ("end_turn", "stop_sequence"):
            if said:
                state.pop("turn_ending", None)
                events.append(_turn_end(state))
            else:
                # A thinking-only entry. Claude Code writes the thinking
                # block as its own line, stamped with the message's stop
                # reason, and the text that actually ends the turn 30 ms
                # later (measured 2026-08-29 09:17:23.632 / .661 on the
                # Boss). Ending the turn here ended it EMPTY, and the
                # answer that followed was taken for a new turn: the
                # Boss's reply to a pushed worker update landed as
                # "typed" and the user never heard it. The text ends the
                # turn; the turn marker, or the next message, is the
                # fallback for a turn that ends in thought alone.
                state["turn_ending"] = True
    elif kind == "system" and entry.get("subtype") == "turn_duration":
        if state.pop("turn_ending", False):
            events.append(_turn_end(state))
    return events


def _resume_filler(entry: dict, kind, message: dict, content) -> bool:
    """The pair `claude --resume` writes on its own, before anyone speaks:
    an isMeta user line "Continue from where you left off." and a
    synthetic assistant "No response requested." (measured on 2.1.268,
    task_3ef16ed0, 2026-09-11 00:31:50Z). Neither is a turn. Read as one,
    the Boss would be told the worker had finished with "No response
    requested." the moment it was resumed. A synthetic API error is a
    real turn end and is kept."""
    texts = [content.strip()] if isinstance(content, str) else [
        block.get("text", "").strip() for block in (content or [])
        if isinstance(block, dict) and block.get("type") == "text"]
    if kind == "user":
        return bool(entry.get("isMeta")) and \
            texts == ["Continue from where you left off."]
    if kind == "assistant" and message.get("model") == "<synthetic>" \
            and not entry.get("isApiErrorMessage"):
        return texts == ["No response requested."]
    return False


def _turn_end(state: dict) -> AgentEvent:
    """The turn's result is what the worker said LAST. A turn is prose
    between tool calls ("Reading the exact code paths.", "Tests green
    (69). Now, one gated sequence...") and then the answer; joined and
    cut at 600 from the front, the answer was the part that fell off,
    and the Boss was told a finish consisted of its opening remarks."""
    texts = state.get("turn_text", [])
    summary = " ".join(texts[-1].split()) if texts else ""
    state["turn_text"] = []
    return AgentEvent(type="completed",
                      summary=keep_end(summary, SUMMARY_CEILING))


def finished_turn(transcript: Path) -> str | None:
    """What the last turn in a transcript said, if it already ended.

    A watcher adopted onto a running worker reads from the END of its
    transcript, so a turn that finished while nobody was reading produced
    no event: the answer sat in the worker's pane, the Boss was never
    told, and the task stayed "running". This reads the whole file
    through the same normalizer the watcher uses and answers whether its
    final state is a finished turn - the finish the adopting caller then
    delivers - or an open one (a user message or mid-turn progress last),
    which the watcher will see end itself.
    """
    state: dict = {}
    last: AgentEvent | None = None
    try:
        with open(transcript, encoding="utf-8") as lines:
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for event in normalize_entry(entry, state):
                    last = event
    except OSError:
        return None
    if state.get("turn_ending"):
        # Ended on thought alone and the file stops there: the marker or
        # next message that would flush it never came. It is over.
        last = _turn_end(state)
    if last is not None and last.type == "completed":
        return last.summary
    return None


class ClaudeCodeAdapter(CliAdapter):
    """Claude Code, exactly as the runtime drove it before the seam."""

    name = "claude-code"
    display = "Claude Code"
    binary_name = "claude"

    # The user answers by voice, relayed as typed text. AskUserQuestion
    # renders an option menu the relay cannot submit (typing moves the
    # highlight, nothing presses enter), the turn never completes, and no
    # event tells the Manager a worker is waiting. Denied, the worker asks
    # in prose and ends its turn, which the Manager does see.
    WORKER_DISALLOWED = ("AskUserQuestion",)
    # Every worker is Opus at medium effort (asked 2026-09-10), on launch
    # and on resume alike, whatever the user's own default is. Not fast
    # mode: that is the Boss's alone. "opus" is the alias, so it follows
    # the current Opus.
    WORKER_MODEL = ("--model=opus", "--effort=medium")

    # The box, as measured off live workers: "❯" and U+00A0, wrapped text
    # indented underneath, then the bottom rule and the "⏵⏵" status line.
    PROMPT_MARKS = ("❯", ">")
    BOX_ENDS = ("─", "⏵", "? for shortcuts")
    # Text that SITS in the box without anyone having typed it: the
    # CLI's own hint, and - measured - the note Claude Code leaves after
    # it queues a message while working. Reading that note as "the user is
    # mid-sentence" would hold back every follow-up after the first one.
    PLACEHOLDERS = ("try \"", "try '", "press up to edit queued messages")
    BUSY = re.compile(r"esc to interrupt", re.I)
    # After a recap Claude Code draws a suggested next prompt in the box.
    SUGGESTS_IN_BOX = True

    def launch_argv(self, prompt: str, permission_mode: str,
                    session_id: str | None = None) -> list[str]:
        # The equals form is load-bearing: --disallowedTools is variadic,
        # and the space form swallows the positional prompt as another
        # tool name - the worker launches idle with no goal (measured on
        # claude 2.1.251).
        argv = [self.binary, "--permission-mode", permission_mode,
                *self.WORKER_MODEL,
                "--disallowedTools=" + ",".join(self.WORKER_DISALLOWED)]
        if session_id:
            argv += ["--session-id", session_id]
        return argv + [prompt]

    def resume_argv(self, session_id: str) -> list[str]:
        return [self.binary, "--resume", session_id, *self.WORKER_MODEL,
                "--disallowedTools=" + ",".join(self.WORKER_DISALLOWED)]

    def prompt_ready(self, screen: str) -> bool:
        return bool(PROMPT_READY.search(screen))

    def approval_prompt(self, screen: str) -> str | None:
        return detect_approval_prompt(screen)

    def startup_dialog(self, screen: str) -> str | None:
        low = screen.lower()
        if "claude in chrome extension detected" in low and \
                "keep browser tools off" in low:
            return "chrome"
        if "trust this folder" in low:
            return "trust"
        if "resume from summary" in low or "resume full session" in low:
            return "resume_picker"
        if "bypass permissions mode" in low and "yes, i accept" in low:
            return "bypass"
        # First-run onboarding: the theme picker, then the security notes.
        # Shown once per machine, and only when no input box is up yet -
        # "press enter to continue" is too common a phrase to act on while
        # the session is accepting text.
        if not PROMPT_READY.search(screen) and (
                "choose the text style" in low
                or ("press enter to continue" in low
                    and "claude code" in low)):
            return "onboarding"
        return None

    # Where Claude Code keeps its projects. Read at call time from the
    # runtime module, which is the name tests (and anyone else) have
    # always patched: `tmux_runtime.CLAUDE_PROJECTS = tmp`. Set it here
    # to override for one adapter.
    projects_root: Path | None = None

    def transcript_dir(self, working_directory: str) -> Path | None:
        root = self.projects_root
        if root is None:
            from . import tmux_runtime      # lazy: it imports this module
            root = tmux_runtime.CLAUDE_PROJECTS
        return root / munge_project_dir(working_directory)

    def normalize(self, entry: dict, state: dict) -> list[AgentEvent]:
        return normalize_entry(entry, state)

    def finished_turn(self, transcript: Path) -> str | None:
        return finished_turn(transcript)


ADAPTERS = {"claude-code": ClaudeCodeAdapter}


def adapter_for(provider: str, binary: str | None = None) -> CliAdapter:
    """The adapter for a provider name, or a bare CliAdapter for one we
    have no knowledge of - which still hosts, types and reads a screen."""
    cls = ADAPTERS.get(provider)
    if cls is None:
        # The screen-driven adapters live in their own module, which
        # imports this one; look them up late.
        for module in (".screen_adapter", ".codex_adapter"):
            try:
                more = __import__(__package__ + module, fromlist=["ADAPTERS"]).ADAPTERS
            except ImportError:     # not shipped in this build
                continue
            if provider in more:
                cls = more[provider]
                break
    if cls is None:
        # Last, a CLI the user described in providers.json.
        from .configured_adapter import CONFIGURED, ConfiguredAdapter
        if provider in CONFIGURED:
            return ConfiguredAdapter(provider, CONFIGURED[provider], binary)
        generic = CliAdapter(binary or provider)
        generic.name = provider
        return generic
    return cls(binary)
