"""GlobalConductor tests, including the v2 acceptance scenarios that can be
verified deterministically (A, B, D, I, J) with scripted backends.

Run with:  python3 -m unittest tests.test_global -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.global_conductor import MANAGER_TOOLS, GlobalConductor
from conductor.manager import FakeManagerBackend
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


def make_repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


class GlobalConductorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = base / "code"
        make_repo(self.roots / "posely")
        make_repo(self.roots / "cheatly")
        self.runtime = FakeCodingAgentRuntime()
        self.backend = FakeManagerBackend()
        self.conductor = GlobalConductor(
            home=base / "home", runtime=self.runtime, manager=self.backend,
            search_roots=[self.roots],
            # The cap's own tests want a small one they can reach.
            max_concurrent_tasks=3,
            workspace_factory=lambda project: FakeWorkspaceManager())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def register(self, name: str) -> str:
        result = self.conductor.register_project(str(self.roots / name))
        return result["project_id"]

    # -- Scenario A: no project selected, named project, create ----------
    def test_scenario_a_create_in_named_project(self) -> None:
        candidates = self.conductor.find_project("Posely")
        self.assertEqual(len(candidates), 1)
        pid = self.conductor.register_project(candidates[0]["path"])["project_id"]
        task = self.run_async(self.conductor.handle_action(
            "create_task", {"project_id": pid, "title": "Fix login",
                            "goal": "Fix the login bug"}))
        self.assertEqual(task.project_id, pid)
        self.assertEqual(task.status, "running")
        self.assertIsNotNone(task.provider_session_id)
        focus = self.conductor.projects.focus()
        self.assertEqual(focus["project_id"], pid)
        self.assertEqual(focus["task_id"], task.id)

    def test_bookkeeping_cannot_fail_a_tool_that_did_its_work(self) -> None:
        """Measured on 2026-09-01: a null focus in global.json crashed
        set_focus at the tail of every tool, create_task reported
        failure after each worker was fully created, and the Boss's
        retries made three workers of one request."""
        pid = self.register("posely")

        def boom(**kwargs) -> None:
            raise TypeError(
                "'NoneType' object does not support item assignment")
        self.conductor.projects.set_focus = boom
        task = self.run_async(self.conductor.handle_action(
            "create_task", {"project_id": pid, "title": "Fix login",
                            "goal": "Fix the login bug"}))
        self.assertEqual(task.status, "running")
        self.assertIsNotNone(task.provider_session_id)
        summary = self.run_async(self.conductor.handle_action(
            "inspect_project", {"project_id": pid}))
        self.assertEqual(len(summary["tasks"]), 1)   # and no duplicate

    # -- Scenario B/I: independent work in a second project ----------------
    def test_scenario_b_and_i_cross_project_turn(self) -> None:
        posely = self.register("posely")
        cheatly = self.register("cheatly")
        login = self.run_async(self.conductor.create_task(
            "Fix login", "goal", project_id=posely))
        # One manager turn: pause Posely's login and start Cheatly billing.
        self.backend.next_actions = [
            ("interrupt_task", {"task_id": login.id}),
            ("create_task", {"project_id": cheatly, "title": "Fix billing",
                             "goal": "Fix checkout billing"}),
        ]
        turn = self.run_async(self.conductor.handle_user_message(
            "Pause the Posely login thing and have Cheatly fix billing.",
            source="test"))
        self.assertEqual([c.tool for c in turn.tool_calls],
                         ["interrupt_task", "create_task"])
        tasks = {t.title: t for t in self.conductor.list_tasks()}
        self.assertEqual(tasks["Fix login"].status, "waiting_for_user")
        self.assertEqual(tasks["Fix billing"].status, "running")
        self.assertEqual(tasks["Fix billing"].project_id, cheatly)

    def test_a_title_past_the_limit_is_cut_and_the_task_still_starts(self) -> None:
        pid = self.register("posely")
        self.run_async(self.conductor.handle_action(
            "create_task", {"project_id": pid, "goal": "g",
                            "title": "Fix transcript spacing around punctuation and noise tags"}))
        (task,) = self.conductor.list_tasks()
        self.assertEqual(task.title, "Fix transcript spacing around")

    # -- Scenario D: global status ------------------------------------------
    def test_scenario_d_global_summary(self) -> None:
        posely = self.register("posely")
        cheatly = self.register("cheatly")
        self.run_async(self.conductor.create_task("Fix login", "g",
                                                  project_id=posely))
        summaries = self.conductor.list_projects()
        by_name = {s["name"]: s for s in summaries}
        self.assertEqual(by_name["posely"]["active_tasks"], 1)
        self.assertEqual(by_name["cheatly"]["active_tasks"], 0)
        context = self.conductor.global_context()
        self.assertIn("posely", context)
        self.assertIn("Recent focus", context)

    # -- Scenario J: restart restores everything -------------------------------
    def test_scenario_j_restart_recovery(self) -> None:
        posely = self.register("posely")
        task = self.run_async(self.conductor.create_task(
            "Fix login", "g", project_id=posely))

        revived = GlobalConductor(
            home=Path(self.tmp.name) / "home",
            runtime=FakeCodingAgentRuntime(),
            search_roots=[self.roots],
            workspace_factory=lambda project: FakeWorkspaceManager())
        report = self.run_async(revived.startup(resume=True))
        self.assertEqual([t.id for t in report["recovered_tasks"]],
                         [task.id])
        recovered = revived.list_tasks()[0]
        self.assertEqual(recovered.project_id, posely)
        self.assertEqual(revived.projects.focus()["task_id"], task.id)

    def test_missing_project_marked_not_deleted(self) -> None:
        gone = make_repo(self.roots / "ghost")
        pid = self.register("ghost")
        import shutil
        shutil.rmtree(gone)
        report = self.run_async(self.conductor.startup())
        self.assertIn(pid, report["missing_projects"])
        self.assertEqual(self.conductor.projects.get(pid).status, "missing")

    def test_task_ids_resolve_without_project_scope(self) -> None:
        posely = self.register("posely")
        cheatly = self.register("cheatly")
        a = self.run_async(self.conductor.create_task("A", "g",
                                                      project_id=posely))
        b = self.run_async(self.conductor.create_task("B", "g",
                                                      project_id=cheatly))
        self.run_async(self.conductor.handle_action(
            "send_to_task", {"task_id": b.id, "message": "hello"}))
        self.assertIn(("send", b.provider_session_id, "hello"),
                      self.runtime.calls)
        self.assertEqual(self.conductor.projects.focus()["project_id"],
                         cheatly)

    def test_unknown_tool_and_task(self) -> None:
        with self.assertRaises(ValueError):
            self.run_async(self.conductor.handle_action("rm_rf", {}))
        with self.assertRaises(KeyError):
            self.run_async(self.conductor.handle_action(
                "inspect_task", {"task_id": "task_missing"}))

    def test_the_tool_surface_is_exactly_this(self) -> None:
        """Named rather than counted: a bare number says the surface grew
        but not what appeared, which is the part worth reviewing."""
        # Removed 2026-09-10: create_task locates the project itself.
        from conductor.boss_tools import REQUIRED_TOOLS, SCHEMAS
        for gone in ("find_project", "register_project"):
            self.assertNotIn(gone, SCHEMAS)
            self.assertNotIn(gone, REQUIRED_TOOLS)
        self.assertEqual(set(MANAGER_TOOLS), {
            # projects
            "list_projects", "inspect_project",
            # tasks
            "create_task", "list_tasks", "inspect_task", "send_to_task",
            "pause_task", "interrupt_task", "resume_task",
            # complete is the user saying the work is over; cancel is the
            # user abandoning it. A worker never decides either.
            "complete_task", "cancel_task",
            "handoff_task_context", "focus_task",
            "approve_task_action", "deny_task_action",
            # supervision, and the open/history split
            "list_subagents", "list_open_sessions", "list_recent_sessions",
            "search_sessions",
            # for the voice's ears only: never spoken, never typed
            "note_for_voice",
            # what the user heard from the voice, when the Boss asks
            "what_the_voice_said", "tell_user",
            # the spoken digest: "what's going on?"
            "situation",
        })

    def test_concurrency_cap(self) -> None:
        posely = self.register("posely")
        for i in range(3):
            self.run_async(self.conductor.create_task(f"t{i}", "g",
                                                      project_id=posely))
        with self.assertRaises(RuntimeError):
            self.run_async(self.conductor.create_task("t3", "g",
                                                      project_id=posely))
        # Pausing one frees a slot: only starting/running count.
        first = self.conductor.list_tasks()[0]
        self.run_async(self.conductor.interrupt_task(first.id))
        task = self.run_async(self.conductor.create_task("t3", "g",
                                                         project_id=posely))
        self.assertEqual(task.status, "running")

    def test_no_cap_when_it_is_zero(self) -> None:
        """boss.MAX_CONCURRENT_TASKS = 0 means the machine's own limits
        decide, so nothing counts busy workers and nothing is refused."""
        self.conductor.max_concurrent_tasks = 0
        posely = self.register("posely")
        for i in range(5):
            task = self.run_async(self.conductor.create_task(
                f"t{i}", "g", project_id=posely))
            self.assertEqual(task.status, "running")

    def test_an_idle_worker_does_not_hold_a_slot(self) -> None:
        """Measured: every launch was refused for twenty minutes while
        the three slots were held by an answered question, a task whose
        PR was merged, and a worker whose window was gone. A task stays
        "running" until someone says complete; the cap is on workers
        actually working."""
        from conductor.agent_events import AgentEvent
        posely = self.register("posely")
        tasks = [self.run_async(self.conductor.create_task(f"t{i}", "g",
                                                           project_id=posely))
                 for i in range(3)]
        with self.assertRaises(RuntimeError) as caught:
            self.run_async(self.conductor.create_task("t3", "g", project_id=posely))
        self.assertIn("3 workers are busy", str(caught.exception))
        # The first worker finishes its turn and sits idle. Nobody says
        # complete: the task is still open, and still not holding a slot.
        self.runtime.emit(tasks[0].provider_session_id, AgentEvent(
            type="completed", summary="Opened PR #12; tests green."))
        state = self.conductor._conductor(posely).subagent_state_dict(tasks[0].id)
        self.assertEqual(state["status"], "idle")
        self.assertNotIn(self.conductor.list_tasks()[0].status,
                         ("completed", "cancelled"))
        fourth = self.run_async(self.conductor.create_task("t3", "g", project_id=posely))
        self.assertEqual(fourth.status, "running")
        # Three are working again: the next one is refused.
        with self.assertRaises(RuntimeError):
            self.run_async(self.conductor.create_task("t4", "g", project_id=posely))

    def test_a_worker_waiting_on_the_user_does_not_hold_a_slot(self) -> None:
        from conductor.agent_events import AgentEvent
        posely = self.register("posely")
        tasks = [self.run_async(self.conductor.create_task(f"t{i}", "g",
                                                           project_id=posely))
                 for i in range(3)]
        self.runtime.emit(tasks[1].provider_session_id, AgentEvent(
            type="needs_input", summary="Which branch?"))
        task = self.run_async(self.conductor.create_task("t3", "g", project_id=posely))
        self.assertEqual(task.status, "running")

    def test_handoff_sends_context_not_transcript(self) -> None:
        posely = self.register("posely")
        cheatly = self.register("cheatly")
        api = self.run_async(self.conductor.create_task(
            "API pagination", "Paginate endpoints", project_id=posely))
        front = self.run_async(self.conductor.create_task(
            "Frontend list view", "Render the list", project_id=cheatly))
        # The API task records a finding through its context.
        from conductor import task_context
        pc = self.conductor._conductor(posely)
        task_context.append_findings(pc.store, api.id,
                                     ["The cursor param is base64"])
        self.run_async(self.conductor.handle_action(
            "handoff_task_context",
            {"from_task_id": api.id, "to_task_id": front.id,
             "note": "use what the API task found"}))
        sends = [c for c in self.runtime.calls if c[0] == "send"]
        self.assertEqual(sends[-1][1], front.provider_session_id)
        self.assertIn("cursor param is base64", sends[-1][2])
        self.assertIn("API pagination", sends[-1][2])
        with self.assertRaises(ValueError):
            self.run_async(self.conductor.handoff_task_context(api.id,
                                                               api.id))

    def test_checkpoint_updates_context(self) -> None:
        from conductor.agent_events import AgentEvent
        posely = self.register("posely")
        task = self.run_async(self.conductor.create_task(
            "Fix login", "g", project_id=posely))
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="checkpoint", summary="Implemented a candidate fix.",
            detail={"findings": ["Refresh completes after redirect"],
                    "next_steps": ["Run auth tests", "Verify Safari"]}))
        pc = self.conductor._conductor(posely)
        context = pc.store.context_path(task.id).read_text()
        self.assertIn("Implemented a candidate fix.", context)
        self.assertIn("Refresh completes after redirect", context)
        self.assertIn("Run auth tests", context)
        # A checkpoint reports progress; it does not end the turn.
        self.assertEqual(pc.store.get(task.id).status, "running")


if __name__ == "__main__":
    unittest.main()
