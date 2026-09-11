#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["claude-agent-sdk>=0.2,<1"]
# ///

"""LIVE approval acceptance test (spec: the decision must reach the exact
live worker and that worker must demonstrably resume).

A real write-enabled Claude worker is told to run a command the policy
refuses to auto-allow (`touch conductor-approval-test.txt`). Expected:

    worker starts -> hits the approval gate -> waiting_for_approval with an
    addressable approval id -> we approve -> the SAME session advances ->
    the file exists -> completed -> same session id throughout.

    env -u ANTHROPIC_API_KEY ./tests/smoke_approval.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conductor.claude_runtime import ClaudeCodeRuntime


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        runtime = ClaudeCodeRuntime(
            allow_write=True, max_turns=6,
            system_prompt="Do exactly what is asked, briefly.")
        approvals: list[dict] = []
        done = asyncio.Event()
        gate = asyncio.Event()

        def on_event(event):
            line = event.summary or event.error or event.question
            print(f"  [{event.type}] {line[:90]}")
            if event.type == "approval_required":
                approvals.append((event.detail or {}).get("approval", {}))
                gate.set()
            if event.type in ("completed", "failed"):
                done.set()

        print("starting write-enabled worker ...")
        sid = await runtime.create_session(
            "task_approval_smoke", tmp,
            "Run this exact shell command with Bash: "
            "touch conductor-approval-test.txt  - then confirm you did.")
        await runtime.subscribe(sid, on_event)

        print("waiting for the approval gate ...")
        await asyncio.wait_for(gate.wait(), timeout=180)
        assert await runtime.reconcile_session(sid) == \
            "waiting_for_approval", "health must say waiting_for_approval"
        pending = await runtime.pending_approvals(sid)
        approval_id = pending[0]["approval_id"]
        print(f"  gate reached: {approvals[0].get('description', '')!r} "
              f"({approval_id})")
        assert not (Path(tmp) / "conductor-approval-test.txt").exists(), \
            "the gated command must not have run yet"

        print("approving into the SAME session ...")
        await runtime.resolve_approval(sid, approval_id, approve=True)
        await asyncio.wait_for(done.wait(), timeout=180)

        assert (Path(tmp) / "conductor-approval-test.txt").exists(), \
            "the worker did not demonstrably advance past the gate"
        assert await runtime.pending_approvals(sid) == []
        executions = await runtime.executions()
        assert len(executions) == 1 and \
            executions[0].provider_session_id == sid, "same session, no dup"
        await runtime.destroy(sid)
        print("approval smoke test passed: gate hit, decision delivered, "
              "same worker resumed and finished")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
