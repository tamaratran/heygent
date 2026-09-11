"""Notification tests: streaming, grouping, dedup, unread, restart,
concurrency - the spec's section 24 cases, all deterministic.

Run with:  python3 -m unittest tests.test_notifications -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.agent_events import AgentEvent
from conductor.global_conductor import GlobalConductor
from conductor.notifications import (POPUP_TYPES, NotificationService,
                                     NotificationStore, TaskNotification)
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


def notif(task_id: str, ntype: str = "milestone", title: str = "t",
          project_id: str = "proj_x", **kw) -> TaskNotification:
    return TaskNotification(project_id=project_id, task_id=task_id,
                            type=ntype, title=title, **kw)


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NotificationStore(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_add_list_unread(self) -> None:
        self.store.add(notif("task_a"))
        self.store.add(notif("task_b"))
        self.assertEqual(self.store.unread_count(), 2)
        self.assertEqual(len(self.store.list(task_id="task_a")), 1)

    def test_progress_coalesces_but_transitions_stay(self) -> None:
        self.store.add(notif("task_a", "progress", "Running tests"))
        self.store.add(notif("task_a", "progress", "Running tests"))
        self.store.add(notif("task_a", "progress", "Running tests"))
        self.assertEqual(len(self.store.list(task_id="task_a")), 1)
        # A semantic transition is never coalesced away.
        self.store.add(notif("task_a", "failed", "Tests failed"))
        self.store.add(notif("task_a", "progress", "Fixing failure"))
        self.store.add(notif("task_a", "milestone", "Tests passed"))
        types = [n.type for n in self.store.list(task_id="task_a")]
        self.assertEqual(types, ["progress", "failed", "progress",
                                 "milestone"])

    def test_read_state(self) -> None:
        first = self.store.add(notif("task_a"))
        self.store.add(notif("task_a"))
        self.store.mark_read(first.id)
        self.assertEqual(self.store.unread_count(), 1)
        self.store.mark_task_read("task_a")
        self.assertEqual(self.store.unread_count(), 0)

    def test_identical_titles_group_by_task_id(self) -> None:
        self.store.add(notif("task_p", title="Fix login",
                             task_title="Fix login", project_id="proj_p"))
        self.store.add(notif("task_c", title="Fix login",
                             task_title="Fix login", project_id="proj_c"))
        groups = self.store.groups()
        self.assertEqual(len(groups), 2)          # never merged by title
        self.assertNotEqual(groups[0]["project_id"], groups[1]["project_id"])

    def test_survives_restart(self) -> None:
        self.store.add(notif("task_a", "needs_input", "Needs your input"))
        reloaded = NotificationStore(self.tmp.name)
        kept = reloaded.list(task_id="task_a", unread_only=True)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].type, "needs_input")

    def test_popup_types_are_the_important_ones(self) -> None:
        self.assertIn("needs_input", POPUP_TYPES)
        self.assertIn("failed", POPUP_TYPES)
        self.assertNotIn("progress", POPUP_TYPES)   # no popup spam
        self.assertNotIn("info", POPUP_TYPES)


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "code" / "posely" / ".git").mkdir(parents=True)
        (base / "code" / "cheatly" / ".git").mkdir(parents=True)
        self.runtime = FakeCodingAgentRuntime()
        self.conductor = GlobalConductor(
            home=base / "home", runtime=self.runtime,
            search_roots=[base / "code"],
            workspace_factory=lambda project: FakeWorkspaceManager())
        self.popups: list[TaskNotification] = []
        self.service = NotificationService(self.conductor,
                                           on_notify=self.popups.append)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def make_task(self, project: str, title: str):
        root = Path(self.tmp.name) / "code" / project
        registered = self.conductor.locator.register(root)
        return asyncio.run(self.conductor.create_task(
            title, "goal", project_id=registered.id))

    def test_streaming_event_reaches_all_surfaces(self) -> None:
        task = self.make_task("posely", "Fix login redirect")
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="checkpoint", summary="Running authentication tests"))
        entries = self.service.store.list(task_id=task.id)
        progress = [n for n in entries if n.type == "progress"]
        self.assertEqual(len(progress), 1)
        self.assertIn("authentication tests", progress[0].body)
        self.assertEqual(progress[0].project_name, "posely")
        self.assertEqual(progress[0].task_title, "Fix login redirect")
        self.assertGreater(self.service.store.unread_count(), 0)

    def test_an_instruction_typed_into_the_window_earns_an_answer(self):
        """The completion dedupe asks "did the user ask for this?", and
        only knew about sends the conductor made. But a PTY-hosted worker
        exists to be typed into: the user types in the Claude Code window,
        the worker answers, and that answer was swallowed as the worker
        talking to itself. The card updated in place - on_activity always
        runs - so the row moved while no notification, bell or spoken line
        ever came. It read as being stuck on the previous one.
        """
        task = self.make_task("posely", "Fix login redirect")
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="completed", summary="oriented; read the code"))
        self.assertEqual(len(self.popups), 1)

        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="progress", summary="> now run the tests",
            detail={"source": "user_message"}))
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="completed", summary="tests pass"))
        self.assertEqual(len(self.popups), 2,
                         "the answer to what the user typed was swallowed")
        self.assertIn("tests pass", self.popups[-1].body)

    def test_the_worker_narrating_itself_still_stays_quiet(self):
        """The other half of the same judgement. A turn end with no
        instruction behind it is not news, and making every one of them
        pop was the behaviour the dedupe exists to prevent."""
        task = self.make_task("posely", "Fix login redirect")
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="completed", summary="oriented"))
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="progress", summary="reading auth.py"))
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="completed", summary="still poking around"))
        self.assertEqual(len(self.popups), 1)

    def test_the_source_survives_the_trip_to_the_bus(self):
        """The exact thing that was lost. The runtime tags a human's
        typing "user_message"; the conductor forwarded only the summary,
        so nothing above it could tell the two apart."""
        seen = []
        self.conductor.bus.subscribe(
            lambda e: seen.append(e) if e.type == "runtime.progress"
            else None)
        task = self.make_task("posely", "Fix login redirect")
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="progress", summary="> run the tests",
            detail={"source": "user_message"}))
        self.assertTrue(seen)
        self.assertEqual(seen[-1].data.get("source"), "user_message")

    def test_needs_input_is_prominent(self) -> None:
        task = self.make_task("posely", "Fix login redirect")
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="needs_input", question="Preserve legacy behavior?"))
        snapshot = self.service.snapshot()
        needs = snapshot["sections"][0]
        self.assertEqual(needs["name"], "NEEDS ATTENTION")
        self.assertEqual(needs["groups"][0]["task_id"], task.id)
        self.assertIn("Preserve legacy behavior?",
                      needs["groups"][0]["notifications"][0]["body"])
        # It stays in NEEDS ATTENTION until read.
        self.service.store.mark_task_read(task.id)
        snapshot = self.service.snapshot()
        self.assertFalse(snapshot["sections"][0]["groups"])

    def test_completion_updates_all_surfaces(self) -> None:
        task = self.make_task("posely", "Fix login redirect")
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="completed", summary="All twelve auth tests passed"))
        # Dropdown entry, canonical status, popup - all agree.
        completions = [n for n in self.service.store.list(task_id=task.id)
                       if n.type == "completed"]
        self.assertEqual(len(completions), 1)
        status = {t.id: t.status for t in self.conductor.list_tasks()}
        self.assertEqual(status[task.id], "waiting_for_user")
        self.assertIn("completed", [p.type for p in self.popups])

    def test_concurrent_sessions_never_cross(self) -> None:
        tasks = [self.make_task("posely", "Fix login"),
                 self.make_task("cheatly", "Fix login")]
        for i, task in enumerate(tasks):
            self.runtime.emit(task.provider_session_id, AgentEvent(
                type="checkpoint", summary=f"update {i}"))
        for i, task in enumerate(tasks):
            entries = [n for n in self.service.store.list(task_id=task.id)
                       if n.type == "progress"]
            self.assertEqual(len(entries), 1)
            self.assertIn(f"update {i}", entries[0].body)
            self.assertEqual(entries[0].project_id, task.project_id)

    def test_snapshot_sections_order_by_state(self) -> None:
        active = self.make_task("posely", "Active work")
        blocked = self.make_task("cheatly", "Blocked work")
        self.runtime.emit(blocked.provider_session_id, AgentEvent(
            type="needs_input", question="Which way?"))
        snapshot = self.service.snapshot()
        by_section = {s["name"]: [g["task_id"] for g in s["groups"]]
                      for s in snapshot["sections"]}
        self.assertIn(blocked.id, by_section["NEEDS ATTENTION"])
        self.assertIn(active.id, by_section["ACTIVE"])


if __name__ == "__main__":
    unittest.main()
