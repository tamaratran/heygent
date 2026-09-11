"""The local trace viewer: debugging without reading raw terminal logs.

Text UI over the local observability store. Answers the spec's questions -
what happened, why, which task, what the Manager saw, what it chose, what the
Conductor executed, how long it took.

    python3 -m conductor.viewer <project_root> traces
    python3 -m conductor.viewer <project_root> trace <trace_id>
    python3 -m conductor.viewer <project_root> tasks
    python3 -m conductor.viewer <project_root> task <task_id>
    python3 -m conductor.viewer <project_root> errors
    python3 -m conductor.viewer <project_root> metrics

Filters: --component, --type, --task apply to `traces` and `trace`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .replay import Trace, list_traces, load_events
from .storage import read_jsonl


def _clock(timestamp: str) -> str:
    return timestamp.split("T")[-1].rstrip("Z") if "T" in timestamp \
        else timestamp


def _matches(event: dict, args) -> bool:
    if args.component and event.get("component") != args.component:
        return False
    if args.type and args.type not in event.get("type", ""):
        return False
    if args.task and event.get("task_id") != args.task:
        return False
    if args.project and event.get("project_id") != args.project:
        return False
    return True


def _state_files(root: Path) -> list[tuple[str, Path]]:
    """(project label, state.json path) pairs for either layout: a v2
    conductor home with projects/, or a legacy in-repo .myconductor/."""
    if (root / "global.json").exists():
        return sorted((p.name, p / "state.json")
                      for p in (root / "projects").glob("proj_*")
                      if (p / "state.json").exists())
    return [("", root / ".myconductor" / "state.json")]


def show_traces(root: Path, args) -> None:
    traces = list_traces(root)
    if args.project:
        traces = [t for t in traces
                  if any(e.get("project_id") == args.project
                         for e in t.events)]
    if args.limit:
        traces = traces[-args.limit:]
    for trace in traces:
        stamp = trace.events[0].get("timestamp", "") if trace.events else ""
        tools = ", ".join(c["tool"] for c in trace.tool_calls) or "-"
        print(f"{trace.trace_id}  {stamp}  [{tools}]")
        if trace.user_message:
            print(f'    "{trace.user_message[:80]}"')


def show_trace(root: Path, trace_id: str, args) -> None:
    trace = next((t for t in list_traces(root) if t.trace_id == trace_id),
                 None)
    if trace is None:
        raise SystemExit(f"no trace {trace_id}")
    print(f"TRACE {trace.trace_id}\n")
    if trace.user_message:
        print(f'User ({trace.source}):\n  "{trace.user_message}"\n')
    if trace.tasks:
        print("Tasks shown to the Manager:")
        for task in trace.tasks:
            print(f"  {task['task_id']:<18} {task['status']:<17} "
                  f"{task['title']}")
        print()
    print("-" * 60)
    for event in trace.events:
        if not _matches(event, args):
            continue
        line = f"{_clock(event.get('timestamp', '')):>9}  {event['type']}"
        if event.get("duration_ms") is not None:
            line += f"  ({event['duration_ms']:.0f} ms)"
        print(line)
        data = event.get("data", {})
        gist = (data.get("tool") and f"{data['tool']}"
                f"({json.dumps(data.get('args', {}))[:60]})") or \
            data.get("summary") or data.get("error") or data.get("reply") or \
            data.get("text") or data.get("prompt")
        if gist:
            print(f"           {str(gist)[:100]}")
    if trace.reply:
        print("-" * 60)
        print(f'Reply:\n  "{trace.reply}"')


def show_tasks(root: Path) -> None:
    for label, state_path in _state_files(root):
        try:
            state = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if label:
            print(f"{label}:")
        for task in state.get("tasks", {}).values():
            indent = "  " if label else ""
            print(f"{indent}{task['id']:<18} {task['status']:<17} "
                  f"{task['title']}")


def show_task(root: Path, task_id: str) -> None:
    for label, state_path in _state_files(root):
        try:
            state = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        task = state.get("tasks", {}).get(task_id)
        if task is None:
            continue
        print(f"{task['title']}  [{task['status']}]"
              + (f"  ({label})" if label else "") + "\n")
        events = read_jsonl(state_path.parent / "tasks" / task_id
                            / "events.jsonl")
        for event in events:
            gist = event.get("summary") or event.get("text") or \
                event.get("error") or ""
            print(f"{event.get('timestamp', ''):>20}  {event['type']:<18} "
                  f"{str(gist)[:70]}")
        return
    raise SystemExit(f"no task {task_id}")


def show_errors(root: Path) -> None:
    groups: dict[tuple, list[dict]] = {}
    for event in load_events(root):
        if event.get("severity") == "error":
            key = (event.get("component"), event.get("type"))
            groups.setdefault(key, []).append(event)
    if not groups:
        print("no errors recorded")
        return
    for (component, event_type), events in sorted(
            groups.items(), key=lambda kv: -len(kv[1])):
        first, last = events[0], events[-1]
        print(f"{component}/{event_type}  count={len(events)}  "
              f"first={first.get('timestamp')}  last={last.get('timestamp')}")
        detail = last.get("data", {}).get("error", "")
        if detail:
            print(f"    last: {str(detail)[:100]}")
        traces = {e.get("trace_id") for e in events if e.get("trace_id")}
        if traces:
            print(f"    traces: {', '.join(sorted(traces)[:5])}")


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = min(len(values) - 1, int(fraction * len(values)))
    return values[index]


def show_metrics(root: Path) -> None:
    events = load_events(root)
    turns = [e for e in events if e["type"] == "manager.turn_completed"]
    calls = [e for e in events if e["type"] == "manager.tool_call"]
    by_tool: dict[str, int] = {}
    for call in calls:
        tool = call.get("data", {}).get("tool", "?")
        by_tool[tool] = by_tool.get(tool, 0) + 1
    clarifications = sum(1 for t in turns if not t.get("data", {}).get("tools"))
    latencies = [t["duration_ms"] for t in turns
                 if t.get("duration_ms") is not None]

    print(f"manager turns:        {len(turns)}")
    print(f"tool calls:           {len(calls)}")
    for tool, count in sorted(by_tool.items(), key=lambda kv: -kv[1]):
        print(f"  {tool:<18} {count}")
    if turns:
        print(f"clarification rate:   {clarifications / len(turns):.0%}")
    if latencies:
        print(f"turn latency p50:     {_percentile(latencies, 0.50):.0f} ms")
        print(f"turn latency p95:     {_percentile(latencies, 0.95):.0f} ms")
        print(f"turn latency p99:     {_percentile(latencies, 0.99):.0f} ms")
    failures = sum(1 for e in events if e["type"] == "task.failed")
    created = sum(1 for e in events if e["type"] == "task.created")
    print(f"tasks created:        {created}")
    print(f"task failures:        {failures}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Local trace viewer.")
    parser.add_argument("root", help="project root (holds .myconductor/)")
    parser.add_argument("view", choices=("traces", "trace", "tasks", "task",
                                         "errors", "metrics"))
    parser.add_argument("target", nargs="?",
                        help="trace id or task id where applicable")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--component")
    parser.add_argument("--type", dest="type")
    parser.add_argument("--task", dest="task")
    parser.add_argument("--project", dest="project")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if args.view == "traces":
        show_traces(root, args)
    elif args.view == "trace":
        if not args.target:
            raise SystemExit("trace view needs a trace id")
        show_trace(root, args.target, args)
    elif args.view == "tasks":
        show_tasks(root)
    elif args.view == "task":
        if not args.target:
            raise SystemExit("task view needs a task id")
        show_task(root, args.target)
    elif args.view == "errors":
        show_errors(root)
    elif args.view == "metrics":
        show_metrics(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
