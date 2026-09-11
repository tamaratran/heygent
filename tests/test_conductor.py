"""Phase 1 tests: task runtime with no model involved.

Run with:  python3 -m unittest tests.test_conductor -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from conductor import TaskStore, Workspace
from conductor import task_context, task_events


class TaskStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = TaskStore(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_state_file_created(self) -> None:
        state = json.loads((self.root / ".myconductor/state.json").read_text())
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["tasks"], {})

    def test_create_get_list(self) -> None:
        task = self.store.create("Fix login redirect",
                                 "Fix intermittent redirect after login")
        self.assertTrue(task.id.startswith("task_"))
        self.assertEqual(task.status, "queued")
        self.assertEqual(self.store.get(task.id).title, "Fix login redirect")
        self.assertEqual([t.id for t in self.store.list()], [task.id])

    def test_update_persists_and_validates(self) -> None:
        task = self.store.create("t", "g")
        updated = self.store.update(
            task.id, status="running", provider_session_id="sess_1",
            workspace=Workspace(path="/tmp/wt", branch="agent/x",
                                isolation_type="git-worktree"))
        self.assertEqual(updated.status, "running")
        with self.assertRaises(ValueError):
            self.store.update(task.id, status="not-a-status")
        with self.assertRaises(KeyError):
            self.store.update("task_missing", status="running")

    def test_reload_survives_restart(self) -> None:
        task = self.store.create("t", "g")
        self.store.update(task.id, status="running",
                          provider_session_id="sess_1")
        reloaded = TaskStore(self.root)          # simulate app restart
        recovered = reloaded.get(task.id)
        self.assertEqual(recovered.status, "running")
        self.assertEqual(recovered.provider_session_id, "sess_1")
        self.assertEqual([t.id for t in reloaded.running_tasks()], [task.id])

    def test_manager_persistence(self) -> None:
        self.store.set_manager("anthropic", "manager_abc")
        self.assertEqual(TaskStore(self.root).manager(),
                         {"provider": "anthropic", "session_id": "manager_abc"})

    def test_atomic_write_leaves_no_tmp(self) -> None:
        self.store.create("t", "g")
        self.assertFalse((self.root / ".myconductor/state.json.tmp").exists())


class TaskContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(self.tmp.name)
        self.task = self.store.create("Fix login", "Fix the redirect")
        task_context.create_initial(self.store, self.task)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_initial_template(self) -> None:
        body = task_context.read(self.store, self.task.id)
        self.assertIn("# Fix login", body)
        self.assertIn("Fix the redirect", body)

    def test_append_instruction(self) -> None:
        task_context.append_instruction(self.store, self.task.id,
                                        "Do not modify OAuth")
        body = task_context.read(self.store, self.task.id)
        self.assertIn("Do not modify OAuth", body)
        self.assertNotIn("(none yet)\n\n## Current Understanding",
                         body.split("## Constraints")[1].split("##")[0])

    def test_update_section(self) -> None:
        task_context.update_section(self.store, self.task.id,
                                    "Current Status", "Testing candidate fix.")
        body = task_context.read(self.store, self.task.id)
        self.assertIn("Testing candidate fix.", body)
        self.assertIn("## Next Steps", body)     # later sections survive

    def test_create_initial_never_clobbers(self) -> None:
        task_context.append_instruction(self.store, self.task.id, "keep me")
        task_context.create_initial(self.store, self.task)
        self.assertIn("keep me", task_context.read(self.store, self.task.id))


class TaskEventsTest(unittest.TestCase):
    def test_append_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(tmp)
            task = store.create("t", "g")
            task_events.append(store, task.id, "task_created")
            task_events.append(store, task.id, "user_instruction",
                               text="Do not modify OAuth")
            events = task_events.read(store, task.id)
            self.assertEqual([e["type"] for e in events],
                             ["task_created", "user_instruction"])
            self.assertTrue(all("timestamp" in e for e in events))


if __name__ == "__main__":
    unittest.main()


class WorkerPromptTest(unittest.TestCase):
    """The first message must say where the worker is.

    A worktree looks nothing like the project the user named out loud. Left
    to infer it, a worker invented a plausible absolute path - under a
    username that did not exist - read nothing, and burned its budget
    re-reading its own workspace root.
    """

    def task(self, workspace=None):
        from conductor.task_types import Task, Workspace
        return Task(id="task_1", project_id="p", title="Add count",
                    goal="Add a count function.",
                    workspace=workspace)

    def test_the_brief_starts_with_the_goal_itself(self) -> None:
        """The user asked for their own prompt, not a wrapper: no title,
        workspace, branch or "Goal:" labels ahead of it."""
        from conductor.conductor import build_worker_prompt
        from conductor.task_types import Workspace
        ws = Workspace(path="/tmp/wt/task_1", branch="agent/task_1")
        prompt = build_worker_prompt(self.task(ws), "", ws)
        self.assertTrue(prompt.startswith("Add a count function.\n\n"))
        for label in ("Your task", "Goal:", "Your workspace", "Your branch",
                      "Add count", "/tmp/wt/task_1"):
            self.assertNotIn(label, prompt)

    def test_a_goalless_task_falls_back_to_its_title(self) -> None:
        from conductor.conductor import build_worker_prompt
        from conductor.task_types import Task
        task = Task(id="task_1", project_id="p", title="Add count", goal="")
        self.assertTrue(build_worker_prompt(task, "").startswith("Add count"))

    def test_every_brief_says_ask_then_wait(self) -> None:
        """A worker that asked which option and then acted on its own
        recommendation had its work reverted when the answer arrived.
        Answers come by voice, relayed, so they are never instant."""
        from conductor.conductor import ASK_THEN_WAIT_RULE, build_worker_prompt
        self.assertIn("end your turn", ASK_THEN_WAIT_RULE)
        prompt = build_worker_prompt(self.task(), "")
        self.assertIn(ASK_THEN_WAIT_RULE, prompt)
