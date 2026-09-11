"""Atomic file primitives for the conductor's local state.

state.json is the single source of truth, so a crash mid-write must never
leave it half-written. Every write goes: temp file -> fsync -> rename, which
is atomic on POSIX filesystems.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def now_iso() -> str:
    """UTC timestamps, one format everywhere."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(event: str, message: str, **fields) -> None:
    """Imported lazily: observability imports this module, so a top-level
    import here would be circular."""
    from .observability import application_log
    application_log("storage", event, message, severity="warning",
                    exc_info=True, **fields)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError):
        # A missing file is ordinary; an unreadable or malformed one is
        # silent data loss the caller cannot see in its default value.
        _log("storage.read_failed", f"could not read {path}", path=str(path))
        return default


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def append_jsonl(path: Path, entry: dict) -> None:
    """Append one line. Appends of a single line are atomic enough for a
    per-task log; the canonical state never lives here."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    entries = []
    skipped = 0
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    skipped += 1
    except FileNotFoundError:
        return entries
    except OSError:
        _log("storage.read_failed", f"could not read {path}", path=str(path))
        return entries
    if skipped:
        _log("storage.jsonl_lines_skipped",
             f"skipped {skipped} malformed line(s) in {path}",
             path=str(path), skipped=skipped)
    return entries
