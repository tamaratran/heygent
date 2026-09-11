#!/usr/bin/env python3
"""Live smoke of the interactive runtime: a real claude process in a real
tmux PTY, driven and observed by the conductor, follow-up into the same
session. Run manually (needs tmux + `claude /login`; unset stale keys):

    env -u ANTHROPIC_API_KEY python3 tests/smoke_interactive.py

Attach yourself while it runs:  tmux attach -t cond_task_smoke
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conductor.tmux_runtime import TmuxClaudeRuntime


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "hello.txt").write_text("the magic word is plover\n")
        runtime = TmuxClaudeRuntime(transcript_dir=str(Path(tmp) / "x"))
        done = asyncio.Event()
        seen = []

        def on_event(event):
            line = event.summary or event.error
            print(f"  [{event.type}] {line[:90]}")
            seen.append(event)
            if event.type in ("completed", "failed"):
                done.set()

        print("starting interactive session in tmux ...")
        sid = await runtime.create_session(
            "task_smoke", tmp,
            "Read hello.txt and tell me the magic word, then stop.")
        print(f"  session {sid}  (attach: tmux attach -t cond_task_smoke)")
        await runtime.subscribe(sid, on_event)
        await asyncio.wait_for(done.wait(), timeout=180)
        assert any("plover" in (e.summary or "").lower() for e in seen), \
            "expected the magic word in the stream"

        print("follow-up into the same PTY ...")
        done.clear()
        await runtime.send(sid, "What was the magic word again? One word.")
        await asyncio.wait_for(done.wait(), timeout=180)

        executions = await runtime.executions()
        assert len(executions) == 1 and executions[0].pty_handle
        await runtime.destroy(sid)
        print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
