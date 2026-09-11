"""Routing eval harness: does natural language reach the right project and
task with the right action?

Each case seeds fake projects and tasks, hands one user message to a Manager
backend, and scores the decisions. Read-only exploration (list/inspect/find,
and register_project) is free - hierarchical context loading means the
Manager may look before it leaps - so scoring keys on the first *mutating*
call: create_task, send_to_task, interrupt_task, resume_task, cancel_task.

Severity weighting follows the spec: acting on the wrong task (or project)
is weighted far more heavily than asking an unnecessary question, because
the product promise is that casual references reliably reach the agent the
user meant.

Case format:

    {"name": ..., "user_message": ...,
     "tasks": [...],                    # seeded into a default project, or
     "projects": [{"project_id": ..., "name": ..., "tasks": [...]}],
     "expected": {"action": ..., "task_id": ...,
                  "also_accept": [...], # other routes that answer as well
                  "project": ...,       # for find/register expectations
                  "semantic_requirements": [...]}}

"also_accept" exists because several read-only routes can be equally right.
"Did the billing one finish?" is answered by inspecting that task or by
searching sessions for it - and once a task is retired from the open roster,
searching is the better route. Scoring one of them as the only correct
answer measures the harness's taste, not the Manager's reference
resolution.

CLI (uses the live Claude Manager; needs `claude /login`):

    python3 -m conductor.evals evals/gold
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .global_conductor import MANAGER_TOOLS, GlobalConductor
from .testing import FakeCodingAgentRuntime, FakeWorkspaceManager

EXPECTED_ACTIONS = MANAGER_TOOLS + ("clarify", "answer_directly")

# The calls that change the world; everything else is free exploration.
MUTATING = ("create_task", "send_to_task", "pause_task", "interrupt_task",
            "resume_task", "cancel_task", "handoff_task_context",
            "approve_task_action", "deny_task_action")
READ_ONLY_EXPECTED = ("list_projects", "find_project", "inspect_project",
                      "register_project", "list_tasks", "list_subagents",
                      "inspect_task",
                      "focus_task")    # navigation: no worker side effects

# Semantically interchangeable suspensions: both halt work resumably. Gold
# cases written before pause existed expect interrupt; either is correct.
_EQUIVALENT = {"interrupt_task": {"pause_task"},
               "pause_task": {"interrupt_task"},
               "list_tasks": {"list_subagents"},
               "list_subagents": {"list_tasks"}}

# Spec section 73 (and v2 section 71): not all mistakes are equal, and
# project misrouting outranks task misrouting.
_WRONG_TASK_SEVERITY = {"send_to_task": 5, "interrupt_task": 7,
                        "cancel_task": 9, "resume_task": 5,
                        "inspect_task": 2}
_WRONG_PROJECT_SEVERITY = {"create_task": 4, "send_to_task": 6,
                           "interrupt_task": 8, "resume_task": 6,
                           "cancel_task": 10}
_FALSE_CLARIFICATION_SEVERITY = 1
_WRONG_ACTION_SEVERITY = 3


@dataclass
class EvalCase:
    name: str
    user_message: str
    expected: dict
    tasks: list[dict] = field(default_factory=list)
    projects: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "EvalCase":
        case = cls(name=data["name"], user_message=data["user_message"],
                   expected=data["expected"], tasks=data.get("tasks", []),
                   projects=data.get("projects", []))
        action = case.expected.get("action")
        if action not in EXPECTED_ACTIONS:
            raise ValueError(f"{case.name}: unknown expected action {action!r}")
        return case


@dataclass
class CaseResult:
    name: str
    passed: bool
    severity: int = 0
    kind: str = ""            # wrong_task | wrong_action | false_clarification
    actual_action: str = ""
    actual_task: str | None = None
    detail: str = ""


def load_cases(path: str | Path) -> list[EvalCase]:
    path = Path(path)
    files = sorted(path.glob("**/*.json")) if path.is_dir() else [path]
    cases = []
    for file in files:
        data = json.loads(file.read_text())
        for item in (data if isinstance(data, list) else [data]):
            cases.append(EvalCase.from_dict(item))
    names = [case.name for case in cases]
    if len(names) != len(set(names)):
        raise ValueError("duplicate eval case names")
    return cases


def _seed_tasks(conductor: GlobalConductor, runtime: FakeCodingAgentRuntime,
                project_id: str, tasks: list[dict]) -> None:
    pc = conductor._conductor(project_id)
    for spec in tasks:
        task = pc.store.create(title=spec["title"],
                               goal=spec.get("goal", spec["title"]),
                               task_id=spec["task_id"])
        session_id = f"sess_{task.id}"
        runtime.statuses[session_id] = "idle"
        runtime.handlers.setdefault(session_id, [])
        pc.store.update(task.id, status=spec.get("status", "running"),
                        provider_session_id=session_id,
                        workspace=pc.workspaces.create(task.id))


def seed(conductor: GlobalConductor, runtime: FakeCodingAgentRuntime,
         case: EvalCase, roots_dir: Path) -> tuple[str, dict[str, str]]:
    """Build the case's world; returns (default project id, name -> id).

    A project spec with "unregistered": true exists only on disk - the
    Manager has to discover and register it (find_project -> register),
    which is how unknown-project cases are expressed.
    """
    specs = case.projects or [{"name": "Default", "tasks": case.tasks}]
    default_pid = ""
    project_map: dict[str, str] = {}
    for spec in specs:
        root = roots_dir / spec["name"].lower().replace(" ", "-")
        (root / ".git").mkdir(parents=True, exist_ok=True)
        if spec.get("unregistered"):
            continue                 # on disk, discoverable, not registered
        project = conductor.projects.register(
            display_name=spec["name"], root_path=root,
            aliases=spec.get("aliases", []))
        project_map[spec["name"]] = project.id
        _seed_tasks(conductor, runtime, project.id, spec.get("tasks", []))
        if not default_pid:
            default_pid = project.id
    return default_pid, project_map


def score(case: EvalCase, calls: list[dict],
          project_map: dict[str, str] | None = None) -> CaseResult:
    """calls = [{"tool": ..., "args": {...}}] in execution order."""
    expected = case.expected
    want_action = expected["action"]
    want_task = expected.get("task_id")
    want_project = (project_map or {}).get(expected.get("project", ""))

    mutating = [c for c in calls if c["tool"] in MUTATING]
    first = mutating[0] if mutating else None
    actual_action = first["tool"] if first else "clarify"
    actual_task = (first["args"].get("task_id")
                   or first["args"].get("to_task_id")) if first else None

    # Clarify and answer_directly both mean "no state changed".
    if want_action in ("clarify", "answer_directly"):
        if first is None:
            return CaseResult(case.name, True, actual_action=want_action)
        severity = _WRONG_TASK_SEVERITY.get(actual_action,
                                            _WRONG_ACTION_SEVERITY)
        return CaseResult(case.name, False, severity=severity,
                          kind="acted_instead_of_clarifying",
                          actual_action=actual_action,
                          actual_task=actual_task,
                          detail=f"expected {want_action}")

    # Read-only expectations: the call must appear, and nothing may mutate.
    if want_action in READ_ONLY_EXPECTED:
        acceptable = ({want_action} | _EQUIVALENT.get(want_action, set())
                      | set(expected.get("also_accept", [])))
        mutating_unexpected = [c for c in mutating
                               if c["tool"] not in acceptable]
        if mutating_unexpected:
            bad = mutating_unexpected[0]
            return CaseResult(case.name, False,
                              severity=_WRONG_ACTION_SEVERITY,
                              kind="wrong_action",
                              actual_action=bad["tool"],
                              actual_task=bad["args"].get("task_id"),
                              detail=f"expected read-only {want_action}")
        matches = [c for c in calls if c["tool"] in acceptable]
        if want_task:
            # An accepted alternative route need not take a task_id at all
            # (search_sessions takes a query), so only hold the primary
            # action to the identity check.
            matches = [c for c in matches
                       if c["tool"] != want_action
                       or c["args"].get("task_id") == want_task]
        if not matches:
            return CaseResult(case.name, False,
                              severity=_FALSE_CLARIFICATION_SEVERITY,
                              kind="false_clarification",
                              actual_action="clarify",
                              detail=f"expected {want_action}")
        return CaseResult(case.name, True, actual_action=want_action,
                          actual_task=want_task)

    # Mutating expectations.
    if first is None:
        return CaseResult(case.name, False,
                          severity=_FALSE_CLARIFICATION_SEVERITY,
                          kind="false_clarification",
                          actual_action="clarify",
                          detail=f"expected {want_action}")
    acceptable = ({want_action} | _EQUIVALENT.get(want_action, set())
                  | set(expected.get("also_accept", [])))
    if actual_action not in acceptable:
        return CaseResult(case.name, False, severity=_WRONG_ACTION_SEVERITY,
                          kind="wrong_action", actual_action=actual_action,
                          actual_task=actual_task,
                          detail=f"expected {want_action}")
    if want_task and actual_task != want_task:
        return CaseResult(case.name, False,
                          severity=_WRONG_TASK_SEVERITY.get(actual_action, 3),
                          kind="wrong_task", actual_action=actual_action,
                          actual_task=actual_task,
                          detail=f"expected {want_task}")

    # create_task takes the project by name (resolved internally); the
    # scorer maps a name back through the seeded registry so both spellings
    # count as the same routing decision.
    actual_project = first["args"].get("project_id")
    if not actual_project and first["args"].get("project"):
        said = str(first["args"]["project"]).lower()
        actual_project = next(
            (pid for name, pid in (project_map or {}).items()
             if name.lower() == said), first["args"]["project"])
    if want_project and actual_project != want_project:
        return CaseResult(
            case.name, False,
            severity=_WRONG_PROJECT_SEVERITY.get(actual_action, 4),
            kind="wrong_project", actual_action=actual_action,
            actual_task=actual_task,
            detail=f"expected project {expected['project']}")

    args_json = json.dumps(first["args"])
    missing = [req for req in expected.get("semantic_requirements", [])
               if req.lower() not in args_json.lower()]
    if missing:
        return CaseResult(case.name, False, severity=4,
                          kind="lost_constraint", actual_action=actual_action,
                          actual_task=actual_task,
                          detail=f"missing: {missing}")
    return CaseResult(case.name, True, actual_action=actual_action,
                      actual_task=actual_task)


async def judge_constraint(constraint: str, message: str) -> bool:
    """Model-based semantic check (spec section 19): used only when the
    literal substring check fails, and only when the runner enables it.
    One cheap no-tools model turn per disputed constraint."""
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  TextBlock, query)
    prompt = (f"Constraint: {constraint!r}\n\nMessage:\n{message}\n\n"
              "Does the message semantically preserve the constraint, even "
              "with different wording? Answer with exactly one word: "
              "yes or no.")
    options = ClaudeAgentOptions(max_turns=1, allowed_tools=[],
                                 disallowed_tools=["Bash", "Read", "Edit",
                                                   "Write", "Glob", "Grep"],
                                 system_prompt="You judge whether a message "
                                               "preserves a constraint. "
                                               "Answer yes or no.")
    text = ""
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text += block.text
    return text.strip().lower().startswith("yes")


async def _rejudge(case: EvalCase, result: CaseResult,
                   calls: list[dict]) -> CaseResult:
    """Rescue lost_constraint failures whose wording differs semantically."""
    mutating = [c for c in calls if c["tool"] in MUTATING]
    if not mutating:
        return result
    args_json = json.dumps(mutating[0]["args"])
    for requirement in case.expected.get("semantic_requirements", []):
        if requirement.lower() in args_json.lower():
            continue
        if not await judge_constraint(requirement, args_json):
            return result             # genuinely lost; keep the failure
    return CaseResult(case.name, True, actual_action=result.actual_action,
                      actual_task=result.actual_task,
                      detail="passed by semantic judge")


async def run_case(case: EvalCase, make_backend,
                   use_judge: bool = False) -> CaseResult:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        runtime = FakeCodingAgentRuntime()
        conductor = GlobalConductor(
            home=tmp / "home", runtime=runtime,
            manager=make_backend(),
            search_roots=[tmp / "roots"],
            workspace_factory=lambda project: FakeWorkspaceManager())
        _, project_map = seed(conductor, runtime, case, tmp / "roots")
        try:
            turn = await conductor.handle_user_message(case.user_message,
                                                       source="test")
        except Exception as exc:
            return CaseResult(case.name, False, severity=3, kind="error",
                              detail=str(exc)[:200])
        finally:
            backend = conductor.manager
            if hasattr(backend, "close"):
                await backend.close()
        calls = [{"tool": c.tool, "args": c.args} for c in turn.tool_calls]
        # Projects the Manager registered during the turn (unknown-project
        # discovery) become scoreable: rebuild the name -> id map after.
        for project in conductor.projects.list():
            for spec in (case.projects or []):
                if spec.get("unregistered") and \
                        project.display_name.lower() == \
                        spec["name"].lower().replace(" ", "-"):
                    project_map.setdefault(spec["name"], project.id)
            project_map.setdefault(project.display_name, project.id)
        result = score(case, calls, project_map)
        if use_judge and not result.passed and \
                result.kind == "lost_constraint":
            result = await _rejudge(case, result, calls)
        return result


async def run_suite(cases: list[EvalCase], make_backend,
                    use_judge: bool = False) -> dict:
    results = [await run_case(case, make_backend, use_judge)
               for case in cases]
    failed = [r for r in results if not r.passed]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "severity_score": sum(r.severity for r in failed),
        "wrong_task": sum(r.kind == "wrong_task" for r in failed),
        "wrong_project": sum(r.kind == "wrong_project" for r in failed),
        "wrong_action": sum(r.kind == "wrong_action" for r in failed),
        "false_clarification": sum(r.kind == "false_clarification"
                                   for r in failed),
        "results": results,
    }


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True,
                              cwd=Path(__file__).parent).stdout.strip()
    except OSError:
        return "unknown"


def main() -> int:
    import argparse

    from .claude_manager import (MANAGER_PROMPT_VERSION,
                                 ClaudeManagerBackend)

    parser = argparse.ArgumentParser(description="Run routing evals against "
                                     "the live Claude Manager.")
    parser.add_argument("path", help="eval file or directory")
    parser.add_argument("--out", default="evals/results",
                        help="directory for the run record")
    parser.add_argument("--judge", action="store_true",
                        help="rescue lost-constraint failures with a "
                             "model-based semantic check")
    args = parser.parse_args()

    cases = load_cases(args.path)
    print(f"{len(cases)} cases from {args.path}")
    report = asyncio.run(run_suite(cases, ClaudeManagerBackend,
                                   use_judge=args.judge))

    for result in report["results"]:
        mark = "pass" if result.passed else "FAIL"
        line = f"  {mark}  {result.name}"
        if not result.passed:
            line += (f"  [{result.kind} sev={result.severity}] "
                     f"got {result.actual_action}"
                     f"{'/' + result.actual_task if result.actual_task else ''}"
                     f" - {result.detail}")
        print(line)

    print(f"\nPassed: {report['passed']}/{report['total']}"
          f"\nWrong task: {report['wrong_task']}"
          f"\nWrong project: {report['wrong_project']}"
          f"\nWrong action: {report['wrong_action']}"
          f"\nFalse clarification: {report['false_clarification']}"
          f"\nSeverity-weighted error score: {report['severity_score']}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": _git_commit(),
        "manager_prompt_version": MANAGER_PROMPT_VERSION,
        "totals": {k: report[k] for k in ("total", "passed", "severity_score",
                                          "wrong_task", "wrong_project",
                                          "wrong_action",
                                          "false_clarification")},
        "cases": [{"name": r.name, "passed": r.passed, "kind": r.kind,
                   "severity": r.severity, "actual_action": r.actual_action,
                   "actual_task": r.actual_task, "detail": r.detail}
                  for r in report["results"]],
    }
    out_path = out_dir / f"run_{int(time.time())}.json"
    out_path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"\nrun record: {out_path}")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
