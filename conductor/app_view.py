"""The Boss's conversation, drawn by us.

A CLI in a PTY owns its pixels; we scrape the screen it chooses to
draw. Driving Codex through its app-server (conductor/codex_app.py)
inverts that - the protocol carries turns, items and approvals, and
nothing renders them until we do. So:

  - a turn is a card, not a scroll of interleaved output;
  - a session the answer mentions gets a toast under that card, with a
    link that opens it - the thing Claude Code's transcript cannot do,
    because a Stop hook's systemMessage is stripped of escape sequences
    (conductor/turn_toast.py says more);
  - an approval is a card with buttons. Codex asks and BLOCKS, so the
    buttons are real: pressing one settles the request the server is
    waiting on. In a terminal a button is a URL - Ghostty, which cmux
    runs, linkifies them - so Accept and Decline are OSC 8 links onto
    the same loopback server the toast links use.

This is a front end, not the Boss. Nothing here is wired into the
running conductor; `python3 -m conductor.app_view "..."` draws one
conversation so the shape can be judged before it replaces anything.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass, field

from .codex_app import Approval, CodexApp, CodexUnavailable
from .jump import JumpServer
from .turn_toast import mentioned, read_sessions

# 256-colour, because a card the user cannot skim is the thing we are
# trying to fix. NO_COLOR is honoured; so is a pipe.
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
BLUE, GREEN, AMBER, RED, GREY = (
    "\033[38;5;75m", "\033[38;5;71m", "\033[38;5;179m",
    "\033[38;5;167m", "\033[38;5;245m")
GLYPH_COLOUR = {"attention": AMBER, "failed": RED, "done": GREEN,
                "working": BLUE}


def link(url: str, label: str) -> str:
    """An OSC 8 hyperlink. We own this terminal, so it survives."""
    if not url:
        return label
    return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"


@dataclass
class Theme:
    colour: bool = True
    width: int = 0                 # 0 = ask the terminal

    def __post_init__(self) -> None:
        if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
            self.colour = False
        if not self.width:
            self.width = min(shutil.get_terminal_size((88, 24)).columns, 96)

    def paint(self, text: str, *codes: str) -> str:
        return f"{''.join(codes)}{text}{RESET}" if self.colour else text


class Card:
    """One box. Built line by line, printed whole - a half-drawn card
    interleaved with the next event is what a scrolling TUI does."""

    def __init__(self, theme: Theme, title: str, accent: str = BLUE) -> None:
        self.theme = theme
        self.title = title
        self.accent = accent
        self.lines: list[str] = []

    def add(self, text: str, *codes: str) -> "Card":
        """Wrapped to the card, except a line that already carries escape
        sequences: slicing one of those by character count cuts an OSC 8
        link in half, and the terminal prints the URL - measured, the
        button row came out as "acceptForSession[ Accept for session ]".
        Lines we build ourselves (buttons, toasts) are short by
        construction, so they pass through whole."""
        width = self.theme.width - 4
        for paragraph in (text or "").splitlines() or [""]:
            if "\033" in paragraph:
                self.lines.append(paragraph)
                continue
            while len(paragraph) > width:
                cut = paragraph.rfind(" ", 0, width)
                cut = cut if cut > width // 2 else width
                self.lines.append(self.theme.paint(paragraph[:cut], *codes))
                paragraph = paragraph[cut:].lstrip()
            self.lines.append(self.theme.paint(paragraph, *codes))
        return self

    def render(self) -> str:
        width = self.theme.width
        head = f"╭─ {self.title} "
        out = [self.theme.paint(head + "─" * max(0, width - _len(head) - 1)
                                + "╮", self.accent)]
        for line in self.lines:
            pad = " " * max(0, width - _len(line) - 4)
            bar = self.theme.paint("│", self.accent)
            out.append(f"{bar} {line}{pad} {bar}")
        out.append(self.theme.paint("╰" + "─" * (width - 2) + "╯", self.accent))
        return "\n".join(out)


def _len(text: str) -> int:
    """Printable width. A CSI colour run ends at "m"; an OSC 8 link
    introducer ends at ESC-backslash and its URL counts for nothing."""
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\033":
            if text[i + 1:i + 2] == "]":            # OSC: ends at ESC \\
                end = text.find("\033\\", i)
                i = len(text) if end < 0 else end + 2
            else:                                    # CSI: ends at a letter
                end = i + 1
                while end < len(text) and not text[end].isalpha():
                    end += 1
                i = end + 1
            continue
        out += 1
        i += 1
    return out


class ConversationView:
    """Draws one Codex conversation. print_ is injected so tests read
    what a user would see."""

    def __init__(self, theme: Theme | None = None, home=None,
                 jump: JumpServer | None = None, print_=print) -> None:
        self.theme = theme or Theme()
        self.home = home
        self.jump = jump
        self.print = print_
        self.pending: dict[str, Approval] = {}

    # -- the pieces ----------------------------------------------------------
    def you(self, text: str) -> None:
        self.print(Card(self.theme, "you", GREY).add(text).render())

    def turn_card(self, answer: str, tools: list[str], model: str,
                  seconds: float) -> None:
        card = Card(self.theme, f"codex · {model}" if model else "codex", BLUE)
        card.add(answer or "(nothing said)")
        for tool in tools[:6]:
            card.add(f"⚙ {tool}", DIM)
        card.add(f"{seconds:.1f}s", DIM)
        self.print(card.render())

    def toast(self, answer: str) -> None:
        """Under the card: the sessions this answer mentioned, linked.

        The same rule the Claude Code toast uses, drawn the way that one
        cannot be - a real link, and colour for the state."""
        rows = read_sessions(self.home) if self.home else []
        for row in mentioned(answer, rows):
            colour = GLYPH_COLOUR.get(row.get("glyph") or "working", BLUE)
            dot = self.theme.paint("●", colour)
            title = self.theme.paint(row.get("title") or row["task_id"], BOLD)
            status = self.theme.paint(f" — {row.get('status') or ''}", DIM)
            # Our own server if we have one: the row's url belongs to
            # whichever run wrote the file, and that port may be gone.
            url = self.jump.url_for(row["task_id"]) if self.jump \
                else (row.get("url") or "")
            open_it = self.theme.paint(link(url, "open"), colour)
            self.print(f"   ↳ {dot} {title}{status}   {open_it}")

    def approval_card(self, approval: Approval) -> None:
        """Codex is blocked on this. The buttons settle it."""
        self.pending[approval.item_id] = approval
        card = Card(self.theme, "codex needs permission", AMBER)
        card.add(approval.question, BOLD)
        if approval.detail:
            card.add(approval.detail, DIM)
        card.add(" ".join(self._button(approval, decision, label)
                          for decision, label in (
                              ("accept", " Accept "),
                              ("acceptForSession", " Accept for session "),
                              ("decline", " Decline "))))
        if self.jump:
            # Terminals only follow a link on ⌘-click; a plain click is
            # a mouse event to the shell. Measured on 2026-08-30: a user
            # clicked Accept, nothing happened, and the demo timed out.
            card.add("⌘-click a button to press it - "
                     "a plain click does nothing", DIM)
        self.print(card.render())

    def _button(self, approval: Approval, decision: str, label: str) -> str:
        url = self.jump.url_for_action(f"{approval.item_id}:{decision}") \
            if self.jump else ""
        painted = self.theme.paint(f"[{label}]",
                                   GREEN if decision != "decline" else RED)
        return link(url, painted)

    async def decide(self, rest: str) -> str:
        """A button was pressed: settle the request Codex is waiting on."""
        item_id, _, decision = rest.partition(":")
        approval = self.pending.get(item_id)
        if approval is None:
            return "That request is no longer waiting."
        approval.answer(decision or "accept")
        self.print(f"   {self.theme.paint('✓ ' + (decision or 'accept'), GREEN)}")
        return f"{decision or 'accept'} sent to Codex."

    # -- one turn ------------------------------------------------------------
    async def run_turn(self, app: CodexApp, text: str) -> str:
        self.you(text)
        started = asyncio.get_running_loop().time()
        answer, tools = "", []
        async for event in app.turn(text):
            if event.kind == "item":
                if event.item_type == "agentMessage":
                    answer = event.text
                elif event.item_type in ("commandExecution", "fileChange",
                                         "mcpToolCall"):
                    tools.append(event.text)
            elif event.kind == "approval":
                self.approval_card(event.data["approval"])
            elif event.kind == "turn_done":
                answer = event.text or answer
            elif event.kind == "error":
                self.print(Card(self.theme, "error", RED).add(event.text).render())
                return ""
        elapsed = asyncio.get_running_loop().time() - started
        self.turn_card(answer, tools, app.model, elapsed)
        self.toast(answer)
        return answer


async def demo(prompt: str, cwd: str, home) -> int:
    """One conversation, drawn. Not the Boss - see the module docstring."""
    view = ConversationView(home=home)
    # persist=False: the demo runs beside a live conductor and must not
    # take over the remembered port its toasts resolve on.
    jump = JumpServer(home, focus=_focus_with_cmux,
                      routes={"/a/": view.decide}, persist=False)
    try:
        await jump.start()
    except OSError:
        jump = None                      # buttons become plain labels
    view.jump = jump
    app = CodexApp(cwd=cwd, on_approval=lambda a: None)   # the card asks
    try:
        await app.start()
    except CodexUnavailable as exc:
        print(f"codex app-server is not available: {exc}", file=sys.stderr)
        return 1
    try:
        await app.start_thread(approval_policy="untrusted",
                               sandbox={"type": "readOnly"})
        await view.run_turn(app, prompt)
    finally:
        await app.stop()
        if jump is not None:
            await jump.stop()
    return 0


async def _focus_with_cmux(task_id: str) -> None:
    """Bring a worker's window forward, for the demo's own links. The
    product does this through focus_task; here it is one cmux call, so
    the demo needs nothing of the conductor running."""
    from .cmux_runtime import CmuxClaudeRuntime
    from .tmux_runtime import session_name
    runtime = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
    runtime.cmux = shutil.which("cmux")
    runtime._password = None
    runtime.places, runtime._workspaces_cache = {}, None
    runtime.transcript = None
    await asyncio.to_thread(runtime._bring_forward, session_name(task_id))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python3 -m conductor.app_view '<prompt>' [cwd]",
              file=sys.stderr)
        return 2
    from pathlib import Path
    home = Path.home() / ".voice-conductor"
    return asyncio.run(demo(argv[0], argv[1] if len(argv) > 1 else os.getcwd(),
                            home))


if __name__ == "__main__":
    raise SystemExit(main())
