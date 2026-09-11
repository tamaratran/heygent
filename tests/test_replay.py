"""Scenario runner, replay engine, trace-to-eval, and viewer plumbing tests.

All scripted - no LLM.

Run with:  python3 -m unittest tests.test_replay -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.conductor import Conductor
from conductor.evals import load_cases, run_case
from conductor.manager import FakeManagerBackend
from conductor.observability import JsonlSink, ObservabilityBus
from conductor.replay import (deterministic_replay, list_traces, load_trace,
                              manager_replay, trace_to_eval)
from conductor.scenarios import Scenario, load_scenarios, run_scenario
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


def make_conductor(root: str) -> tuple[Conductor, FakeManagerBackend]:
    bus = ObservabilityBus()
    bus.subscribe(JsonlSink(root))
    backend = FakeManagerBackend()
    conductor = Conductor(root, FakeCodingAgentRuntime(),
                          workspaces=FakeWorkspaceManager(), bus=bus,
                          manager=backend)
    return conductor, backend


class ScenarioRunnerTest(unittest.TestCase):
    SCENARIO = Scenario(
        name="create-then-constrain",
        tasks=[],
        turns=[
            {"user_message": "Fix the login redirect.",
             "expected": {"action": "create_task"}},
            {"user_message": "Tell it not to touch OAuth.",
             "expected": {"action": "send_to_task", "created_in_turn": 0,
                          "semantic_requirements": ["OAuth"]},
             "expect_status": "running"},
        ])

    def test_correct_backend_passes(self) -> None:
        class Scripted(FakeManagerBackend):
            async def handle(self, text, conductor):
                if "Fix the login" in text:
                    self.next_actions = [("create_task",
                                          {"title": "Login redirect",
                                           "goal": "Fix redirect"})]
                else:
                    task = conductor.list_tasks()[0]
                    self.next_actions = [("send_to_task",
                                          {"task_id": task.id,
                                           "message": "Do not touch OAuth"})]
                return await super().handle(text, conductor)

        outcome = asyncio.run(run_scenario(self.SCENARIO, Scripted))
        self.assertTrue(outcome.passed,
                        [r.detail for r in outcome.turn_results])

    def test_wrong_turn_fails_scenario(self) -> None:
        class Lazy(FakeManagerBackend):
            async def handle(self, text, conductor):
                return await super().handle(text, conductor)  # never acts

        outcome = asyncio.run(run_scenario(self.SCENARIO, Lazy))
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.turn_results[0].kind, "false_clarification")

    def test_shipped_scenarios_load(self) -> None:
        scenarios = load_scenarios("evals/scenarios")
        self.assertGreaterEqual(len(scenarios), 20)
        self.assertGreaterEqual(sum(len(s.turns) for s in scenarios), 50)


class ReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conductor, self.backend = make_conductor(self.tmp.name)
        # One real interaction, recorded through the JSONL sink.
        self.backend.next_actions = [("create_task",
                                      {"title": "Fix login",
                                       "goal": "Fix the redirect"})]
        self.backend.next_reply = "Started."
        asyncio.run(self.conductor.handle_user_message(
            "Fix the login redirect.", source="test"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_trace_is_self_contained(self) -> None:
        traces = list_traces(self.tmp.name)
        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self.assertEqual(trace.user_message, "Fix the login redirect.")
        self.assertEqual(trace.tool_calls[0]["tool"], "create_task")
        self.assertEqual(trace.reply, "Started.")

    def test_deterministic_replay(self) -> None:
        trace = list_traces(self.tmp.name)[0]
        results = asyncio.run(deterministic_replay(trace))
        self.assertTrue(all(r["ok"] for r in results), results)

    def test_manager_replay_compares_decisions(self) -> None:
        trace = list_traces(self.tmp.name)[0]

        def make_backend():
            backend = FakeManagerBackend()
            backend.next_actions = [("create_task",
                                     {"title": "Fix login",
                                      "goal": "redo"})]
            return backend

        comparison = asyncio.run(manager_replay(trace, make_backend))
        self.assertEqual(comparison["original"][0]["tool"], "create_task")
        self.assertEqual(comparison["replayed"][0]["tool"], "create_task")

    def test_trace_to_eval_roundtrip(self) -> None:
        trace = list_traces(self.tmp.name)[0]
        out_dir = Path(self.tmp.name) / "regressions"
        path = trace_to_eval(trace, "login redirect regression", out_dir)
        cases = load_cases(path)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].expected["action"], "create_task")

        # The generated case must actually run and pass with a backend that
        # repeats the recorded decision.
        class Repeat(FakeManagerBackend):
            async def handle(self, text, conductor):
                self.next_actions = [("create_task",
                                      {"title": "t", "goal": "g"})]
                return await super().handle(text, conductor)

        result = asyncio.run(run_case(cases[0], Repeat))
        self.assertTrue(result.passed, result.detail)

    def test_missing_trace_raises(self) -> None:
        with self.assertRaises(KeyError):
            load_trace(self.tmp.name, "trace_nope")


class AdversarialSuiteShapeTest(unittest.TestCase):
    def test_all_suites_load_together(self) -> None:
        gold = load_cases("evals/gold")
        adversarial = load_cases("evals/adversarial")
        self.assertGreaterEqual(len(gold), 100)
        self.assertGreaterEqual(len(adversarial), 10)


if __name__ == "__main__":
    unittest.main()
