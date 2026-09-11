"""A worker's history, from its transcript rather than its pane.

The Boss window draws a worker by capturing its tmux pane. That is the
right thing for "what is on screen now" and the wrong thing for "what
happened": a full-screen CLI repaints the same rows in place, so
nothing ever scrolls off the top and tmux's history stays empty.
Measured 2026-09-01 on a live worker, after #167 turned the alternate
screen off:

    alternate-screen off · alternate_on=0 · history_size=0

and a controlled pane proved the mechanism - a TUI that repaints in
place leaves history_size=0, while ordinary scrolling output leaves
106. `capture-pane -S -2000` cannot reach into a buffer nothing wrote.

The history exists, though: every CLI we host keeps a transcript, and
the runtime already knows how to read one - that is how cards and the
Boss's own timeline are fed. So the terminal view gets its scrollback
from there, and keeps the pane capture for the live screen.
"""

from __future__ import annotations

import json
from pathlib import Path

from .cli_adapter import adapter_for
from .observability import application_log

# How much of a worker's past the view is given. Long enough to scroll
# through what it did, short enough to send on every open.
TURNS_KEPT = 60
LINE_CHARS = 400


def task_dir(home, task_id: str) -> Path | None:
    """Where the conductor keeps this task, across every project."""
    root = Path(home) / "projects"
    if not root.is_dir():
        return None
    for project in root.iterdir():
        candidate = project / "tasks" / task_id
        if candidate.is_dir():
            return candidate
    return None


def transcript_of(home, task_id: str) -> tuple[Path | None, str]:
    """The worker's own log, and the provider that wrote it."""
    folder = task_dir(home, task_id)
    if folder is None:
        return None, ""
    try:
        record = json.loads((folder / "subagent.json").read_text())
    except (OSError, ValueError):
        return None, ""
    session_id = record.get("provider_session_id") or ""
    provider = record.get("provider") or "claude-code"
    if not session_id:
        return None, provider
    workspace = record.get("workspace") or {}
    cwd = workspace.get("path") if isinstance(workspace, dict) else None
    if not cwd:
        cwd = str(Path(home) / "workspaces" / folder.parent.parent.name / task_id)
    try:
        return adapter_for(provider).transcript_for(cwd, session_id), provider
    except Exception:
        return None, provider


def events_of(home, task_id: str) -> list:
    """The worker's transcript as AgentEvents, oldest first."""
    path, provider = transcript_of(home, task_id)
    if path is None or not path.is_file():
        return []
    adapter = adapter_for(provider)
    state: dict = {}
    events = []
    try:
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                events.extend(adapter.normalize(entry, state))
    except OSError:
        application_log("ui", "worker_history.unreadable",
                        f"could not read {task_id}'s transcript",
                        severity="debug", task_id=task_id)
        return []
    return events


def lines(home, task_id: str, turns: int = TURNS_KEPT) -> list[tuple[str, str]]:
    """(kind, text) per line, the way the view should draw them:

        you      what the user or the Boss sent it
        said     what the worker answered
        tool     a command, a file it read, a search
        end      a turn boundary
    """
    out: list[tuple[str, str]] = []
    for event in events_of(home, task_id):
        text = " ".join(str(event.summary or "").split())[:LINE_CHARS]
        if event.type == "completed":
            out.append(("end", text))
        elif event.type == "failed":
            out.append(("end", f"failed: {event.error}"[:LINE_CHARS]))
        elif event.type != "progress":
            continue
        elif (event.detail or {}).get("source") == "user_message":
            out.append(("you", text[2:] if text.startswith("> ") else text))
        elif (event.detail or {}).get("tool"):
            out.append(("tool", text))
        elif text:
            out.append(("said", text))
    # The tail: a worker that has run all day should not send its
    # morning on every open.
    ends = [i for i, (kind, _) in enumerate(out) if kind == "end"]
    if len(ends) > turns:
        out = out[ends[-turns - 1] + 1:]
    return out
