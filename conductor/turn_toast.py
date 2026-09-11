"""A toast under the Boss's turn, linking the sessions it mentioned.

When the Boss says "I've asked the PR 81 worker to re-run the tests",
the user wants that worker one click away, right there under the
answer - not hunted for in a list of twenty windows.

Claude Code renders exactly one thing under a finished turn that the
model does not see: a Stop hook's `systemMessage`. Measured on 2.1.251:

    ⏺ four
      ⎿  Stop says: ✓ List open PRs - finished · http://127.0.0.1:8977/s/task_x

Its limits shape everything here. The hook's plain stdout is discarded -
stdout has to be one JSON object or nothing is shown at all. The text is
plain: OSC 8 hyperlinks and colour are stripped, so the link is a bare
URL, which terminals (Ghostty, which cmux runs) linkify themselves. And
the model must not be told any of it, so `additionalContext` stays
empty: this is for the person reading.

The hook runs as its own process, started by Claude Code, with no access
to the conductor - so the conductor leaves it what it needs in a file
(`write_sessions`) and the hook reads it. Stdlib only, and no package
imports on the script path: it is run as a plain file.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# What a mention looks like: the id we gave the task, whole or the hex
# tail on its own ("task_00eac099" or "00eac099"). A title has to be
# distinctive before it counts - "PR" and "tests" are in every answer.
TITLE_MIN_CHARS = 12
# One turn's answer can mention everything; a toast that lists ten
# sessions is a wall, not a signpost.
MOST = 3
GLYPHS = {"attention": "!", "failed": "x", "done": "+", "working": "~"}


def sessions_path(home) -> Path:
    return Path(home) / "boss" / "turn_sessions.json"


def write_sessions(home, rows: list[dict]) -> None:
    """What the hook may link to, newest first. The conductor owns this
    file; the hook only reads it."""
    path = sessions_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows))
    tmp.replace(path)                    # a half-written file is never read


def read_sessions(home) -> list[dict]:
    try:
        rows = json.loads(sessions_path(home).read_text())
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def _tail(task_id: str) -> str:
    return task_id.split("_", 1)[-1]


def mentioned(text: str, rows: list[dict]) -> list[dict]:
    """The sessions this answer is about, in the order they are named.

    By id first, because the Boss quotes ids. By title only when the
    title is long enough to mean one thing - a worker called "Tests"
    would otherwise match every answer that says the word.
    """
    low = " ".join(str(text or "").split()).lower()
    if not low:
        return []
    found: list[tuple[int, dict]] = []
    for row in rows:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        where = low.find(task_id.lower())
        if where < 0:
            tail = _tail(task_id).lower()
            where = low.find(tail) if len(tail) >= 6 else -1
        if where < 0:
            title = " ".join(str(row.get("title") or "").split()).lower()
            if len(title) >= TITLE_MIN_CHARS:
                where = low.find(title)
        if where >= 0:
            found.append((where, row))
    found.sort(key=lambda pair: pair[0])
    return [row for _, row in found[:MOST]]


def toast(rows: list[dict]) -> str:
    """The line (or lines) shown under the turn. Plain text: no colour,
    no escape sequences - Claude Code strips them."""
    lines = []
    for row in rows:
        mark = GLYPHS.get(row.get("glyph") or "working", "~")
        title = " ".join(str(row.get("title") or row.get("task_id")).split())
        status = " ".join(str(row.get("status") or "").split())
        link = row.get("url") or ""
        line = f"{mark} {title}"
        if status:
            line += f" - {status}"
        if link:
            line += f" · {link}"
        lines.append(line)
    return "\n".join(lines)


def for_message(text: str, rows: list[dict]) -> str:
    return toast(mentioned(text, rows))


def main(argv: list[str] | None = None) -> int:
    """The hook itself: stdin is Claude Code's Stop payload, stdout is
    one JSON object or nothing.

    Silence is the common case and has to be cheap and safe: an answer
    that mentions no session, an unreadable file, a payload shaped some
    other way - all of them print nothing and exit 0. A hook that fails
    loudly would put an error under every turn the Boss takes.
    """
    argv = sys.argv[1:] if argv is None else argv
    home = Path(argv[0]) if argv else Path.home() / ".voice-conductor"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    said = payload.get("last_assistant_message") or ""
    text = for_message(said, read_sessions(home))
    if text:
        print(json.dumps({"systemMessage": text}))
    return 0


if __name__ == "__main__":       # run as a file: no package on the path
    raise SystemExit(main())
