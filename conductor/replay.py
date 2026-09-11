"""Replay: a trace from real usage becomes a debug run or a regression eval.

A trace is self-contained: manager.turn_started carries the user message and
the exact task registry the Manager saw, manager.tool_call carries what it
decided. That allows three things without the original session:

    deterministic_replay  - re-execute the recorded tool calls against a
                            fresh fake Conductor (tests Conductor behaviour)
    manager_replay        - same input, current Manager (tests routing
                            regressions against what happened originally)
    trace_to_eval         - freeze the trace as a permanent regression case

Every real routing bug should end its life as a file in evals/regressions/.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .global_conductor import GlobalConductor
from .testing import FakeCodingAgentRuntime, FakeWorkspaceManager

_EVENT_DIRS = (Path(".myconductor/observability/events"),
               Path("observability/events"))    # legacy layout, v2 home


@dataclass
class Trace:
    trace_id: str
    user_message: str = ""
    source: str = ""
    tasks: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)   # {tool, args}
    reply: str = ""
    events: list[dict] = field(default_factory=list)


def load_events(project_root: str | Path) -> list[dict]:
    events = []
    for events_dir in _EVENT_DIRS:
        directory = Path(project_root) / events_dir
        for file in sorted(directory.glob("*.jsonl")):
            for line in file.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    return events


def list_traces(project_root: str | Path) -> list[Trace]:
    by_id: dict[str, Trace] = {}
    for event in load_events(project_root):
        trace_id = event.get("trace_id")
        if not trace_id:
            continue
        trace = by_id.setdefault(trace_id, Trace(trace_id))
        trace.events.append(event)
        data = event.get("data", {})
        if event["type"] == "manager.turn_started":
            trace.user_message = data.get("text", "")
            trace.source = data.get("source", "")
            trace.tasks = data.get("tasks", [])
        elif event["type"] == "manager.tool_call":
            trace.tool_calls.append({"tool": data.get("tool"),
                                     "args": data.get("args", {})})
        elif event["type"] == "manager.turn_completed":
            trace.reply = data.get("reply", "")
        # A trace that never reached the Manager - small talk, or the
        # single-session front end - is still a real interaction, so let the
        # voice events headline it rather than showing an empty row.
        elif event["type"] == "voice.utterance_completed":
            trace.user_message = trace.user_message or data.get("text", "")
            trace.source = trace.source or "voice"
        elif event["type"] == "voice.reply_spoken":
            trace.reply = trace.reply or data.get("text", "")
    return list(by_id.values())


def load_trace(project_root: str | Path, trace_id: str) -> Trace:
    for trace in list_traces(project_root):
        if trace.trace_id == trace_id:
            return trace
    raise KeyError(f"no trace {trace_id}")


def _fresh_conductor(trace: Trace, manager=None):
    """A fake-backed GlobalConductor seeded with the registry the trace saw."""
    from .evals import EvalCase, seed
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    runtime = FakeCodingAgentRuntime()
    conductor = GlobalConductor(
        home=root / "home", runtime=runtime, manager=manager,
        search_roots=[root / "roots"],
        workspace_factory=lambda project: FakeWorkspaceManager())
    shim = EvalCase(name="replay", user_message=trace.user_message,
                    expected={"action": "clarify"}, tasks=trace.tasks)
    default_pid, _ = seed(conductor, runtime, shim, root / "roots")
    return conductor, tmp, default_pid


async def deterministic_replay(trace: Trace) -> list[dict]:
    """Re-run the recorded decisions; report per-call success."""
    conductor, tmp, default_pid = _fresh_conductor(trace)
    results = []
    with tmp:
        for call in trace.tool_calls:
            args = dict(call["args"])
            # Traces recorded before projects existed lack a project scope.
            if call["tool"] == "create_task" and "project_id" not in args:
                args["project_id"] = default_pid
            try:
                await conductor.handle_action(call["tool"], args)
                results.append({**call, "ok": True})
            except Exception as exc:
                results.append({**call, "ok": False, "error": str(exc)})
    return results


async def manager_replay(trace: Trace, make_backend) -> dict:
    """Same user message and registry, the current Manager. Returns the
    original and new decisions side by side for comparison."""
    conductor, tmp, _ = _fresh_conductor(trace, manager=make_backend())
    with tmp:
        turn = await conductor.handle_user_message(trace.user_message,
                                                   source="test")
        backend = conductor.manager
        if hasattr(backend, "close"):
            await backend.close()
    return {
        "original": trace.tool_calls,
        "replayed": [{"tool": c.tool, "args": c.args}
                     for c in turn.tool_calls],
        "reply": turn.reply,
    }


def trace_to_eval(trace: Trace, name: str,
                  out_dir: str | Path = "evals/regressions",
                  expected: dict | None = None) -> Path:
    """Freeze a trace as a regression eval.

    By default the recorded first tool call becomes the expectation - use
    that when the trace was *correct* behaviour worth pinning. When the
    trace is a bug, pass `expected` with what should have happened.
    """
    if expected is None:
        if not trace.tool_calls:
            expected = {"action": "clarify"}
        else:
            first = trace.tool_calls[0]
            expected = {"action": first["tool"]}
            if first["args"].get("task_id"):
                expected["task_id"] = first["args"]["task_id"]
    case = {
        "name": name,
        "tasks": trace.tasks,
        "user_message": trace.user_message,
        "expected": expected,
    }
    # Validate before writing: a malformed regression case helps nobody.
    from .evals import EvalCase
    EvalCase.from_dict(case)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    path = out_dir / f"{slug}.json"
    path.write_text(json.dumps([case], indent=2) + "\n")
    return path
