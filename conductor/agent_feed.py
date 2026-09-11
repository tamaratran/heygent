"""`claude agents --json`: what the provider says its sessions are doing.

A supported, scriptable status feed - the CLI's own words: "for scripting;
does not require a TTY". It reports every local claude session, interactive
and background, with a pid, a cwd, a session id and a live status.

This is a better source for "is this worker actually working?" than what we
had. Status used to be inferred from a transcript file: a worker parked on
a prompt still had a recent .jsonl, so it looked alive, and a wedged one
was only caught when the sweep noticed its PTY had gone. The provider
already knows the difference and will say so.

It answers status, not events. Completions, approvals and questions still
come from the transcript stream, which carries content this does not.

Deliberately tolerant: a missing binary, a timeout, or an unparseable
answer yields nothing rather than raising. The callers use this to decide
whether a worker is busy, and "I could not tell" must never read as "it is
gone" - that is how a live worker gets replaced underneath the user.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

# The CLI's status values, mapped onto AGENT_STATUSES.
_STATUS = {"busy": "running", "working": "running", "running": "running",
           "idle": "idle", "waiting": "idle",
           "starting": "starting", "completed": "idle"}

DEFAULT_TIMEOUT_S = 20.0


async def sessions(claude: str | None = None,
                   timeout: float = DEFAULT_TIMEOUT_S,
                   include_completed: bool = True) -> list[dict]:
    """Every local claude session the provider can see. [] when unknown."""
    binary = claude or shutil.which("claude") or "claude"
    args = [binary, "agents", "--json"]
    if include_completed:
        args.append("--all")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_env())
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, OSError):
        return []
    if proc.returncode != 0:
        return []
    try:
        rows = json.loads(out.decode() or "[]")
    except json.JSONDecodeError:
        return []
    return rows if isinstance(rows, list) else []


def _same_dir(a: str, b: str) -> bool:
    """macOS reports /private/var where callers pass /var, and the feed
    resolves symlinks while a workspace path may not."""
    if not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a.rstrip("/") == b.rstrip("/")


def find(rows: list[dict], *, cwd: str | None = None,
         pid: int | None = None, session_id: str | None = None) -> dict | None:
    """The row for one worker, by whichever handle the caller holds."""
    for row in rows:
        if pid is not None and row.get("pid") == pid:
            return row
        if session_id and row.get("sessionId") == session_id:
            return row
        if cwd and _same_dir(str(row.get("cwd", "")), cwd):
            return row
    return None


def status_of(row: dict | None) -> str:
    """One of AGENT_STATUSES. An absent row is "disconnected" only because
    the caller asked about a session the provider does not list at all -
    callers that cannot distinguish "gone" from "not asked" should not use
    this to retire anything."""
    if not row:
        return "disconnected"
    raw = str(row.get("state") or row.get("status") or "").lower()
    return _STATUS.get(raw, "running" if raw else "idle")


def _env() -> dict:
    import os
    env = dict(os.environ)
    # A stale key takes the API path and 401s; the stored login is what the
    # feed reads.
    env.pop("ANTHROPIC_API_KEY", None)
    # The voice's key; see tmux_runtime.CONDUCTOR_SECRETS.
    env.pop("OPENAI_API_KEY", None)
    return env
