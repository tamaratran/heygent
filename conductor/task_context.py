"""Per-task semantic memory: tasks/<id>/context.md.

Not a transcript. It holds only what a replacement worker (or the Manager, or
the UI) needs to pick the task up: goal, constraints, findings, status, next
steps. Update it on meaningful semantic changes, not on every tool call.
"""

from __future__ import annotations

from .storage import now_iso
from .task_store import TaskStore
from .task_types import Task

_TEMPLATE = """\
# {title}

## Goal

{goal}

## Constraints

(none yet)

## Current Understanding

(nothing recorded yet)

## Important Findings

(none yet)

## Current Status

{status}

## Next Steps

(none yet)
"""


def create_initial(store: TaskStore, task: Task) -> None:
    path = store.context_path(task.id)
    if path.exists():
        return                       # never clobber recovered context
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TEMPLATE.format(title=task.title, goal=task.goal,
                                     status=task.status))


def read(store: TaskStore, task_id: str) -> str:
    try:
        return store.context_path(task_id).read_text()
    except OSError:
        return ""


def append_instruction(store: TaskStore, task_id: str, text: str) -> None:
    """Record a user instruction/constraint durably.

    A meaningful instruction must reach both the live provider session and
    this file, so a replacement worker started from context alone still
    honours it.
    """
    path = store.context_path(task_id)
    body = read(store, task_id)
    entry = f"- ({now_iso()}) {text.strip()}\n"
    marker = "## Constraints\n"
    if marker in body:
        head, _, tail = body.partition(marker)
        tail = tail.replace("(none yet)\n", "", 1)
        body = head + marker + "\n" + entry + tail.lstrip("\n")
    else:
        body += f"\n## Constraints\n\n{entry}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def append_findings(store: TaskStore, task_id: str,
                    findings: list[str]) -> None:
    """Add discoveries to Important Findings without erasing earlier ones."""
    path = store.context_path(task_id)
    body = read(store, task_id)
    marker = "## Important Findings\n"
    entries = "".join(f"- {finding.strip()}\n" for finding in findings[:8])
    if marker in body:
        head, _, tail = body.partition(marker)
        tail = tail.replace("(none yet)\n", "", 1)
        body = head + marker + "\n" + entries + tail.lstrip("\n")
    else:
        body += f"\n{marker}\n{entries}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def update_section(store: TaskStore, task_id: str, section: str,
                   text: str) -> None:
    """Replace one `## section` body, e.g. Current Status or Next Steps."""
    path = store.context_path(task_id)
    body = read(store, task_id)
    marker = f"## {section}\n"
    if marker not in body:
        body += f"\n{marker}\n{text.strip()}\n"
    else:
        head, _, rest = body.partition(marker)
        _, sep, tail = rest.partition("\n## ")
        body = head + marker + "\n" + text.strip() + "\n"
        if sep:
            body += "\n## " + tail
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
