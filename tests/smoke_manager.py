#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["claude-agent-sdk>=0.2,<1"]
# ///

"""Live Manager v2 smoke test: real Claude routing across projects, fake
everything else.

No project is registered up front. Three turns through one persistent
Manager session:

  1. "Fix the login redirect bug in posely."
       -> find/register posely, create_task there
  2. "Tell the login one not to touch OAuth."
       -> send_to_task(same task)
  3. "While that runs, fix double-charged invoices in cheatly."
       -> resolve the second project, create_task there

Uses your `claude /login`; no coding agent runs.

    ./tests/smoke_manager.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conductor.claude_manager import ClaudeManagerBackend
from conductor.global_conductor import GlobalConductor
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        roots = base / "code"
        for name in ("posely", "cheatly"):
            (roots / name / ".git").mkdir(parents=True)

        backend = ClaudeManagerBackend()
        conductor = GlobalConductor(
            home=base / "home", runtime=FakeCodingAgentRuntime(),
            manager=backend, search_roots=[roots],
            workspace_factory=lambda project: FakeWorkspaceManager())
        try:
            print("turn 1: create in unknown project ...")
            turn = await conductor.handle_user_message(
                "Fix the login redirect bug in posely - users bounce back "
                "to the homepage after logging in.", source="test")
            print(f"  tools: {[(c.tool, c.args) for c in turn.tool_calls]}")
            print(f"  reply: {turn.reply[:120]}")
            creates = [c for c in turn.tool_calls if c.tool == "create_task"]
            assert creates, "expected create_task"
            projects = {p.display_name.lower(): p
                        for p in conductor.projects.list()}
            assert "posely" in projects, "posely was not registered"
            task = conductor.list_tasks()[0]
            assert task.project_id == projects["posely"].id, "wrong project"

            print("turn 2: follow-up ...")
            turn = await conductor.handle_user_message(
                "Tell the login one not to touch OAuth.", source="test")
            print(f"  tools: {[(c.tool, c.args) for c in turn.tool_calls]}")
            print(f"  reply: {turn.reply[:120]}")
            sends = [c for c in turn.tool_calls if c.tool == "send_to_task"]
            assert sends and sends[0].args["task_id"] == task.id, \
                "expected send_to_task to the login task"
            assert "oauth" in sends[0].args["message"].lower(), \
                "constraint lost"

            print("turn 3: second project ...")
            turn = await conductor.handle_user_message(
                "While that runs, fix the double-charged invoices in "
                "cheatly.", source="test")
            print(f"  tools: {[(c.tool, c.args) for c in turn.tool_calls]}")
            print(f"  reply: {turn.reply[:120]}")
            creates = [c for c in turn.tool_calls if c.tool == "create_task"]
            assert creates, "expected create_task in cheatly"
            projects = {p.display_name.lower(): p
                        for p in conductor.projects.list()}
            assert "cheatly" in projects, "cheatly was not registered"
            new_tasks = [t for t in conductor.list_tasks()
                         if t.project_id == projects["cheatly"].id]
            assert new_tasks, "no task in cheatly"
            print("smoke test passed")
        finally:
            await backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
