"""The Boss's reply as it is written, read off its screen.

Claude Code writes its transcript a content block at a time, so the
transcript alone gives the window a whole paragraph at once. Its screen
shows every line the moment it is typed (asked 2026-09-10: "can I at
least do line by line"). The draft is both:

    committed   the paragraphs the transcript has delivered, whole and
                exact - never taken from the screen
    tail        the lines on screen after the last committed paragraph:
                the one still being written

Measured on Claude Code 2.1.268 (fullscreen, focus view, an 80x24 pane):

    ❯ start a worker on the login bug          <- the user's line
    ⏺ Starting a worker on it.                 <- prose: "⏺ " then "  "
      Called boss 2 times                      <- a finished tool group
    ⏺ Finding *                                <- a running tool; the ⏺
      ⎿  "*"                                      blinks off and on
    ✻ Clauding… (10s · ↓ 186 tokens)           <- status, column 0
      ⎿  Tip: ...                              <- belongs to the status
                        Fast mode disabled     <- a right-aligned notice
    ──────────────────────────────────────     <- the input box
    ❯
    ──────────────────────────────────────

The screen is 24 rows, so a fast reply scrolls lines past between two
reads; those arrive when their paragraph is committed. A layout this
cannot read gives no tail at all, and the draft is the committed
paragraphs - what the window showed before.
"""

from __future__ import annotations

import re

RULE = "─"
# A finished or running tool group in focus view.
_TOOL_LINE = re.compile(r"^(Called|Calling) (\S+|\d+ tools)( \d+ times)?…?"
                        r"( \(ctrl\+o to expand\))?$"
                        # a running tool: "Searching for 1 pattern…"
                        r"|^[A-Z][a-z]+ing [^.!?]{0,60}…$")


def _alnum(text: str) -> str:
    """What a paragraph and its rendering share: markdown marks, bullets,
    wrapping and indentation all fall away."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def conversation(screen: str) -> list[str] | None:
    """The rows above the input box, or None when there is no input box
    to find - not the layout this reads."""
    rows = screen.rstrip("\n").split("\n")
    for at in range(len(rows) - 1, 0, -1):
        if rows[at].startswith("❯") and rows[at - 1].startswith(RULE * 8):
            return rows[:at - 1]
    return None


def prompt_text(screen: str) -> str:
    """The user's last line on screen, with its wrapped rows."""
    rows = conversation(screen) or []
    last = max((i for i, row in enumerate(rows) if row.startswith("❯ ")),
               default=-1)
    if last < 0:
        return ""
    said = [rows[last][2:]]
    for row in rows[last + 1:]:
        if not (row.startswith("  ") and row.strip()):
            break
        said.append(row)
    return " ".join(said)


def reply_lines(screen: str) -> list[str] | None:
    """The prose on screen after the user's last line, de-indented, with
    status, notices and tool rows removed. None: layout not recognised."""
    rows = conversation(screen)
    if rows is None:
        return None
    last_prompt = max((i for i, row in enumerate(rows)
                       if row.startswith("❯ ")), default=-1)
    rows = rows[last_prompt + 1:]
    if last_prompt >= 0:
        # The user's line wraps onto indented rows; they are not prose.
        while rows and rows[0].startswith("  ") and rows[0].strip():
            rows = rows[1:]
    out: list[str] = []
    chrome = False             # inside a status line's indented follow-ons
    tool = False               # inside a tool's ⎿ output
    for at, row in enumerate(rows):
        if not row.strip():
            chrome = tool = False
            out.append("")
            continue
        if row.startswith("⏺ "):
            body = row[2:]
            chrome = tool = False
        elif row.startswith("  "):
            if chrome or tool:
                continue
            body = row[2:]
            if len(row) - len(row.lstrip(" ")) >= 20:
                continue       # a right-aligned notice
        else:
            chrome = True      # status, spinner, anything at column 0
            continue
        stripped = body.strip()
        if stripped.startswith("⎿"):
            tool = True
            continue
        following = rows[at + 1] if at + 1 < len(rows) else ""
        if following.strip().startswith("⎿") or \
                _TOOL_LINE.match(stripped) or "(ctrl+o to expand)" in stripped:
            tool = following.strip().startswith("⎿")
            continue
        out.append(body.rstrip())
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return out


def live_tail(lines: list[str], committed: list[str]) -> list[str]:
    """The lines after the end of the last committed paragraph.

    Not on screen, it either scrolled away - every line is newer - or
    the screen is a moment behind the transcript, still showing an
    earlier paragraph's end: nothing on it is newer yet."""
    if not committed:
        return lines
    seen = ""
    starts: list[int] = []      # where each line begins within `seen`
    for line in lines:
        starts.append(len(seen))
        seen += _alnum(line)
    end = _alnum(committed[-1])[-40:]
    found = seen.rfind(end) if end else -1
    if found < 0:
        behind = any(_alnum(p)[-40:] and _alnum(p)[-40:] in seen
                     for p in committed[:-1])
        return [] if behind else lines
    stop = found + len(end)
    after = next((i for i, start in enumerate(starts) if start >= stop),
                 len(lines))
    tail = lines[after:]
    while tail and not tail[0]:
        tail.pop(0)
    return tail


def merge(old: list[str], new: list[str]) -> list[str]:
    """The tail read before, carried forward by the tail read now.

    Lines scroll off the top between two reads: where the new read
    begins partway into the old one, the old lines above it stay. And
    Claude Code redraws: measured, one frame of a streaming reply came
    back blank. A read that shows nothing the old tail did not already
    hold takes nothing away."""
    if not old:
        return new
    if not new or _alnum("".join(new)) in _alnum("".join(old)):
        return old
    for at in range(len(old)):
        overlap = old[at:]
        if len(overlap) > len(new) or not any(line for line in overlap):
            continue
        head, last = overlap[:-1], overlap[-1]
        if new[:len(head)] == head and \
                new[len(overlap) - 1].startswith(last):
            return old[:at] + new
    return old + [""] + new


class ReplyDraft:
    """One turn's reply so far: committed paragraphs, then the screen's
    tail. feed() and commit() answer the new draft, or None if unchanged."""

    # How much of the user's words must be on screen to know the reply
    # below them is this turn's, not the one before it.
    ANCHOR_CHARS = 30

    def __init__(self, said: str = "") -> None:
        self.committed: list[str] = []
        self.tail: list[str] = []
        self._shown = ""
        self._anchor = _alnum(said)[:self.ANCHOR_CHARS]
        self._anchored = not self._anchor

    def commit(self, paragraph: str) -> str | None:
        paragraph = paragraph.strip()
        if not paragraph:
            return None
        self.committed.append(paragraph)
        # The tail was this paragraph being written: what follows its
        # end, if anything, stays; the rest is now committed.
        end = _alnum(paragraph)[-40:]
        seen = _alnum("".join(self.tail))
        self.tail = live_tail(self.tail, self.committed) \
            if end and end in seen else []
        return self._changed()

    def feed(self, screen: str) -> str | None:
        if not self._anchored:
            # Until this turn's words are on screen, what is below the
            # last prompt is the previous reply.
            if self._anchor not in _alnum(prompt_text(screen)):
                return None
            self._anchored = True
        lines = reply_lines(screen)
        if lines is None:
            return None
        self.tail = merge(self.tail, live_tail(lines, self.committed))
        return self._changed()

    def text(self) -> str:
        parts = list(self.committed)
        if self.tail:
            parts.append("\n".join(self.tail))
        return "\n\n".join(parts)

    def _changed(self) -> str | None:
        text = self.text()
        if text == self._shown:
            return None
        self._shown = text
        return text
