"""Multi-turn scenario evals: one persistent Manager across a conversation.

Single-turn routing evals cannot catch conversational failures - "stop the
other one" only means something after two tasks exist. A scenario seeds a
registry, then plays user turns against one Conductor and one Manager
session, scoring every turn and checking task state afterwards.

Turn expectations may reference tasks created earlier in the scenario with
"created_in_turn": N, since generated task ids are not knowable in advance.

CLI (live Claude Manager):

    python3 -m conductor.scenarios evals/scenarios
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .evals import EXPECTED_ACTIONS, CaseResult, score, seed
from .evals import EvalCase as _ShimCase
from .global_conductor import GlobalConductor
from .testing import FakeCodingAgentRuntime, FakeWorkspaceManager


@dataclass
class Scenario:
    name: str
    turns: list[dict]
    tasks: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Scenario":
        scenario = cls(name=data["name"], turns=data["turns"],
                       tasks=data.get("tasks", []))
        for i, turn in enumerate(scenario.turns):
            action = turn["expected"].get("action")
            if action not in EXPECTED_ACTIONS:
                raise ValueError(f"{scenario.name} turn {i}: "
                                 f"unknown action {action!r}")
        return scenario


@dataclass
class ScenarioResult:
    name: str
    turn_results: list[CaseResult]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.turn_results)

    @property
    def severity(self) -> int:
        return sum(r.severity for r in self.turn_results)


def load_scenarios(path: str | Path) -> list[Scenario]:
    path = Path(path)
    files = sorted(path.glob("**/*.json")) if path.is_dir() else [path]
    scenarios = []
    for file in files:
        data = json.loads(file.read_text())
        for item in (data if isinstance(data, list) else [data]):
            scenarios.append(Scenario.from_dict(item))
    return scenarios


def _resolve_expected_task(expected: dict, created: dict[int, str]) -> dict:
    """Turn a created_in_turn reference into the concrete task id."""
    if "created_in_turn" in expected:
        expected = dict(expected)
        turn_no = expected.pop("created_in_turn")
        expected["task_id"] = created.get(turn_no, f"<no task from "
                                                   f"turn {turn_no}>")
    return expected


async def run_scenario(scenario: Scenario, make_backend) -> ScenarioResult:
    results: list[CaseResult] = []
    created: dict[int, str] = {}       # turn index -> task id it created
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        runtime = FakeCodingAgentRuntime()
        conductor = GlobalConductor(
            home=tmp / "home", runtime=runtime, manager=make_backend(),
            search_roots=[tmp / "roots"],
            workspace_factory=lambda project: FakeWorkspaceManager())
        shim = _ShimCase(name=scenario.name, user_message="",
                         expected={"action": "clarify"},
                         tasks=scenario.tasks)
        _, project_map = seed(conductor, runtime, shim, tmp / "roots")
        for index, turn_spec in enumerate(scenario.turns):
            before = {t.id for t in conductor.list_tasks()}
            try:
                turn = await conductor.handle_user_message(
                    turn_spec["user_message"], source="test")
            except Exception as exc:
                results.append(CaseResult(
                    f"{scenario.name}[{index}]", False, severity=3,
                    kind="error", detail=str(exc)[:200]))
                continue
            new_ids = [t.id for t in conductor.list_tasks()
                       if t.id not in before]
            if new_ids:
                created[index] = new_ids[0]

            expected = _resolve_expected_task(turn_spec["expected"], created)
            turn_case = _ShimCase(name=f"{scenario.name}[{index}]",
                                  user_message=turn_spec["user_message"],
                                  expected=expected, tasks=scenario.tasks)
            calls = [{"tool": c.tool, "args": c.args}
                     for c in turn.tool_calls]
            result = score(turn_case, calls, project_map)
            results.append(result)

            # Optional post-turn state assertion, e.g. the interrupted task
            # really is waiting_for_user in canonical state.
            want_status = turn_spec.get("expect_status")
            if result.passed and want_status:
                target = expected.get("task_id") or created.get(index)
                task = next((t for t in conductor.list_tasks()
                             if t.id == target), None) if target else None
                if task is None or task.status != want_status:
                    results[-1] = CaseResult(
                        shim.name, False, severity=3, kind="state_mismatch",
                        actual_action=result.actual_action,
                        detail=f"status {task.status if task else 'missing'} "
                               f"!= {want_status}")
        backend = conductor.manager
        if hasattr(backend, "close"):
            await backend.close()
    return ScenarioResult(scenario.name, results)


async def run_scenarios(scenarios: list[Scenario], make_backend) -> dict:
    outcomes = [await run_scenario(s, make_backend) for s in scenarios]
    return {
        "total": len(outcomes),
        "passed": sum(o.passed for o in outcomes),
        "turns": sum(len(o.turn_results) for o in outcomes),
        "turns_passed": sum(r.passed for o in outcomes
                            for r in o.turn_results),
        "severity_score": sum(o.severity for o in outcomes),
        "outcomes": outcomes,
    }


def main() -> int:
    import argparse

    from .claude_manager import ClaudeManagerBackend

    parser = argparse.ArgumentParser(description="Run multi-turn scenarios "
                                     "against the live Claude Manager.")
    parser.add_argument("path", help="scenario file or directory")
    args = parser.parse_args()

    scenarios = load_scenarios(args.path)
    print(f"{len(scenarios)} scenarios from {args.path}")
    report = asyncio.run(run_scenarios(scenarios, ClaudeManagerBackend))
    for outcome in report["outcomes"]:
        mark = "pass" if outcome.passed else "FAIL"
        print(f"  {mark}  {outcome.name}")
        for result in outcome.turn_results:
            if not result.passed:
                print(f"        {result.name}: [{result.kind} "
                      f"sev={result.severity}] got {result.actual_action} "
                      f"- {result.detail}")
    print(f"\nScenarios: {report['passed']}/{report['total']}"
          f"\nTurns: {report['turns_passed']}/{report['turns']}"
          f"\nSeverity-weighted error score: {report['severity_score']}")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
