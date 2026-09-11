"""Manager seam + eval harness tests, all with scripted backends (no LLM).

Run with:  python3 -m unittest tests.test_manager -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest

from conductor.conductor import Conductor
from conductor.evals import EvalCase, load_cases, run_case, run_suite
from conductor.manager import FakeManagerBackend, ManagerBackend, ManagerTurn
from conductor.observability import ObservabilityBus
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


class UsersWordsTest(unittest.TestCase):
    def test_a_delegation_with_the_block_splits(self):
        from conductor.manager import (VERBATIM_FOOTER, VERBATIM_HEADER,
                                       users_words)
        text = (f"run the tests\n\n{VERBATIM_HEADER}\nyes , correct\n"
                f"{VERBATIM_FOOTER} The line above ...")
        self.assertEqual(users_words(text), ("run the tests", "yes , correct"))

    def test_a_typed_turn_has_no_words_of_its_own(self):
        from conductor.manager import users_words
        self.assertEqual(users_words("  start the login fix "),
                         ("start the login fix", ""))


class HandleUserMessageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = ObservabilityBus()
        self.events = []
        self.bus.subscribe(self.events.append)
        self.backend = FakeManagerBackend()
        self.conductor = Conductor(self.tmp.name, FakeCodingAgentRuntime(),
                                   workspaces=FakeWorkspaceManager(),
                                   bus=self.bus, manager=self.backend)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_scripted_decision_executes(self) -> None:
        self.backend.next_actions = [("create_task",
                                      {"title": "Fix login",
                                       "goal": "Fix the redirect"})]
        self.backend.next_reply = "Started a task for the login fix."
        turn = asyncio.run(self.conductor.handle_user_message(
            "Fix the login redirect.", source="test"))
        self.assertEqual(turn.reply, "Started a task for the login fix.")
        self.assertEqual(turn.tool_calls[0].tool, "create_task")
        tasks = self.conductor.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].status, "running")

    def test_turn_events_emitted(self) -> None:
        asyncio.run(self.conductor.handle_user_message("hello",
                                                       source="test"))
        types = [e.type for e in self.events]
        self.assertIn("manager.turn_started", types)
        self.assertIn("manager.turn_completed", types)
        # The whole turn shares one trace.
        self.assertEqual(len({e.trace_id for e in self.events}), 1)

    def test_no_backend_raises(self) -> None:
        conductor = Conductor(self.tmp.name, FakeCodingAgentRuntime(),
                              workspaces=FakeWorkspaceManager())
        with self.assertRaises(RuntimeError):
            asyncio.run(conductor.handle_user_message("hi"))


class _OneShotBackend(ManagerBackend):
    """Answers every message with one fixed decision."""

    def __init__(self, tool: str | None, args: dict | None = None) -> None:
        self.tool, self.args = tool, args or {}

    async def handle(self, text, conductor) -> ManagerTurn:
        turn = ManagerTurn(reply="ok")
        if self.tool:
            result = await conductor.handle_action(self.tool, self.args)
            from conductor.manager import ToolCall
            turn.tool_calls.append(ToolCall(self.tool, self.args,
                                            str(result)[:100]))
        return turn


class EvalHarnessTest(unittest.TestCase):
    CASE = EvalCase(
        name="oauth",
        tasks=[{"task_id": "task_login", "title": "Fix login redirect",
                "goal": "Fix redirect", "status": "running"},
               {"task_id": "task_settings", "title": "Settings layout",
                "goal": "Responsive settings", "status": "running"}],
        user_message="Tell the login one not to touch OAuth.",
        expected={"action": "send_to_task", "task_id": "task_login",
                  "semantic_requirements": ["OAuth"]})

    def test_correct_decision_passes(self) -> None:
        result = asyncio.run(run_case(
            self.CASE, lambda: _OneShotBackend(
                "send_to_task", {"task_id": "task_login",
                                 "message": "Do not modify OAuth."})))
        self.assertTrue(result.passed, result.detail)

    def test_wrong_task_scores_severity(self) -> None:
        result = asyncio.run(run_case(
            self.CASE, lambda: _OneShotBackend(
                "send_to_task", {"task_id": "task_settings",
                                 "message": "Do not modify OAuth."})))
        self.assertFalse(result.passed)
        self.assertEqual(result.kind, "wrong_task")
        self.assertEqual(result.severity, 5)

    def test_lost_constraint_detected(self) -> None:
        result = asyncio.run(run_case(
            self.CASE, lambda: _OneShotBackend(
                "send_to_task", {"task_id": "task_login",
                                 "message": "Keep going."})))
        self.assertFalse(result.passed)
        self.assertEqual(result.kind, "lost_constraint")

    def test_false_clarification(self) -> None:
        result = asyncio.run(run_case(self.CASE,
                                      lambda: _OneShotBackend(None)))
        self.assertFalse(result.passed)
        self.assertEqual(result.kind, "false_clarification")
        self.assertEqual(result.severity, 1)

    def test_clarify_expected(self) -> None:
        case = EvalCase(name="ambig", tasks=self.CASE.tasks,
                        user_message="Stop it.",
                        expected={"action": "clarify"})
        good = asyncio.run(run_case(case, lambda: _OneShotBackend(None)))
        self.assertTrue(good.passed)
        bad = asyncio.run(run_case(case, lambda: _OneShotBackend(
            "cancel_task", {"task_id": "task_login"})))
        self.assertFalse(bad.passed)
        self.assertEqual(bad.severity, 9)     # wrong cancel is near-maximal

    def test_suite_totals(self) -> None:
        report = asyncio.run(run_suite(
            [self.CASE], lambda: _OneShotBackend(
                "send_to_task", {"task_id": "task_login",
                                 "message": "no OAuth changes"})))
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["severity_score"], 0)


class GoldSuiteShapeTest(unittest.TestCase):
    def test_gold_cases_load_and_are_consistent(self) -> None:
        cases = load_cases("evals/gold")
        self.assertGreaterEqual(len(cases), 100)
        for case in cases:
            task_ids = {t["task_id"] for t in case.tasks}
            for project in case.projects:
                task_ids |= {t["task_id"] for t in project.get("tasks", [])}
            expected_task = case.expected.get("task_id")
            if expected_task:
                self.assertIn(expected_task, task_ids,
                              f"{case.name} expects an unseeded task")
            expected_project = case.expected.get("project")
            if expected_project:
                self.assertIn(expected_project,
                              {p["name"] for p in case.projects},
                              f"{case.name} expects an unseeded project")


if __name__ == "__main__":
    unittest.main()
