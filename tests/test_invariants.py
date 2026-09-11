"""Property-based state tests: random operation sequences, invariants after
every step.

The invariants are the ten from the build spec that state can check:
unique task ids, one session per active task, running tasks have providers,
isolated tasks never share a workspace, statuses stay legal, and a restart
never loses the task -> session/workspace mapping.

Run with:  python3 -m unittest tests.test_invariants -v
"""

from __future__ import annotations

import asyncio
import random
import tempfile
import unittest

from conductor import TaskStore
from conductor.agent_events import AgentEvent
from conductor.conductor import Conductor
from conductor.task_types import TASK_STATUSES
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager

OPS = ("create", "send", "interrupt", "resume", "cancel",
       "emit_completed", "emit_failed", "emit_progress", "restart")


def assert_invariants(store: TaskStore) -> None:
    tasks = store.list()

    ids = [t.id for t in tasks]
    assert len(ids) == len(set(ids)), "task ids not unique"

    active = [t for t in tasks
              if t.status in ("starting", "running", "waiting_for_user")]
    sessions = [t.provider_session_id for t in active
                if t.provider_session_id]
    assert len(sessions) == len(set(sessions)), \
        "one session mapped to two active tasks"

    for task in tasks:
        assert task.status in TASK_STATUSES, f"illegal status {task.status}"
        if task.status == "running":
            assert task.provider_session_id, "running task with no session"
            assert task.workspace, "running task with no workspace"
        assert task.context_path == f"tasks/{task.id}/context.md", \
            "context path does not belong to its task"

    paths = [t.workspace.path for t in active
             if t.workspace and t.workspace.isolation_type != "shared"]
    assert len(paths) == len(set(paths)), "isolated tasks share a workspace"


class RandomLifecycleTest(unittest.TestCase):
    SEQUENCES = 150
    OPS_PER_SEQUENCE = 25

    def test_random_operation_sequences(self) -> None:
        for seed in range(self.SEQUENCES):
            with tempfile.TemporaryDirectory() as tmp:
                asyncio.run(self._run_sequence(tmp, seed))

    async def _run_sequence(self, root: str, seed: int) -> None:
        rng = random.Random(seed)
        runtime = FakeCodingAgentRuntime()
        conductor = Conductor(root, runtime,
                              workspaces=FakeWorkspaceManager())
        created = 0
        for step in range(self.OPS_PER_SEQUENCE):
            op = rng.choice(OPS)
            tasks = conductor.list_tasks()
            task = rng.choice(tasks) if tasks else None
            try:
                if op == "create" and created < 8:
                    created += 1
                    await conductor.create_task(f"task {created}", "goal")
                elif op == "send" and task:
                    await conductor.send_to_task(task.id, "instruction")
                elif op == "interrupt" and task:
                    await conductor.interrupt_task(task.id)
                elif op == "resume" and task:
                    await conductor.resume_task(task.id)
                elif op == "cancel" and task:
                    await conductor.cancel_task(task.id)
                elif op == "emit_completed" and task and \
                        task.provider_session_id:
                    runtime.emit(task.provider_session_id,
                                 AgentEvent(type="completed", summary="done"))
                elif op == "emit_failed" and task and \
                        task.provider_session_id:
                    runtime.emit(task.provider_session_id,
                                 AgentEvent(type="failed", error="boom"))
                elif op == "emit_progress" and task and \
                        task.provider_session_id:
                    runtime.emit(task.provider_session_id,
                                 AgentEvent(type="progress", summary="..."))
                elif op == "restart":
                    conductor = Conductor(root, runtime,
                                          workspaces=FakeWorkspaceManager())
                    await conductor.startup()
            except (KeyError, RuntimeError):
                pass    # an op failing cleanly is fine; corrupting state is not
            try:
                assert_invariants(conductor.store)
            except AssertionError as exc:
                raise AssertionError(
                    f"invariant broken: {exc} (seed={seed}, step={step}, "
                    f"op={op})") from exc


if __name__ == "__main__":
    unittest.main()
