#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["claude-agent-sdk>=0.2,<1"]
# ///

"""Phase 2 smoke test: one real Claude Code session through the runtime.

Creates a session in a temp directory, sends a follow-up into the same
session, checks status transitions, and tears down. Read-only tools; uses
your existing `claude /login`. Run from the repo root:

    ./tests/smoke_claude_runtime.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conductor.agent_events import AgentEvent
from conductor.claude_runtime import ClaudeCodeRuntime


async def main() -> int:
    runtime = ClaudeCodeRuntime(
        allow_write=False, max_turns=4,
        system_prompt="Answer in one short sentence of plain prose.")
    done = asyncio.Event()

    def on_event(event: AgentEvent) -> None:
        line = event.summary or event.error or event.question
        print(f"  [{event.type}] {line[:90]}")
        if event.type in ("completed", "failed"):
            done.set()

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "hello.txt").write_text("the magic word is plover\n")

        print("creating session...")
        session_id = await runtime.create_session(
            task_id="task_smoke", working_directory=tmp,
            initial_prompt="Read hello.txt and tell me the magic word.")
        print(f"  session {session_id}")
        unsubscribe = await runtime.subscribe(session_id, on_event)

        await asyncio.wait_for(done.wait(), timeout=120)
        assert await runtime.get_status(session_id) == "idle", "not idle"

        print("sending follow-up into the same session...")
        done.clear()
        await runtime.send(session_id,
                           "What was the magic word again? One word only.")
        assert await runtime.get_status(session_id) == "running"
        await asyncio.wait_for(done.wait(), timeout=120)

        unsubscribe()
        await runtime.destroy(session_id)
        assert await runtime.get_status(session_id) == "disconnected"
        print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
