"""Per-task history: tasks/<id>/events.jsonl, append-only.

This is the debug and audit record. It is never read back to make decisions -
state.json is current truth, context.md is semantic memory.
"""

from __future__ import annotations

from .storage import append_jsonl, now_iso, read_jsonl
from .task_store import TaskStore


def append(store: TaskStore, task_id: str, event_type: str, **fields) -> dict:
    entry = {"type": event_type, "timestamp": now_iso(), **fields}
    append_jsonl(store.events_path(task_id), entry)
    return entry


def read(store: TaskStore, task_id: str) -> list[dict]:
    return read_jsonl(store.events_path(task_id))
