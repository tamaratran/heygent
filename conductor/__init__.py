"""The local Conductor: deterministic task, session, and workspace state.

The models are allowed to be intelligent and nondeterministic. Everything in
this package is boring and deterministic - task IDs, lifecycle, persistence,
and the mapping from tasks to provider sessions all live here, never in a
model's memory.

Layers, from the spec:

    state.json       = authoritative state        (task_store)
    context.md       = semantic memory            (task_context)
    events.jsonl     = history                    (task_events)
    provider session = detailed working conversation (claude_runtime)

`ClaudeCodeRuntime` is imported lazily - `from conductor.claude_runtime
import ClaudeCodeRuntime` - so that Phase 1 code and its tests never load the
Claude SDK.
"""

from . import instance          # the one-conductor lock and the GUI lease
from .agent_events import AgentEvent
from .conductor import MANAGER_TOOLS, Conductor
from .global_conductor import GlobalConductor
from .locator import ProjectCandidate, ProjectLocator
from .manager import FakeManagerBackend, ManagerBackend, ManagerTurn, ToolCall
from .projects import Project, ProjectStore
from .observability import (ConsoleSink, JsonlSink, LoggingSink,
                            ObservabilityBus, ObservabilityEvent,
                            application_log, configure_logging,
                            current_log_path, current_run, current_trace,
                            drain_subprocess_stderr,
                            install_asyncio_exception_handler, new_run,
                            start_loop_stall_monitor,
                            new_trace, prune_old_logs)
from .runtime import CodingAgentRuntime
from .task_store import CorruptStateError, TaskStore
from .task_types import TASK_STATUSES, Task, Workspace
from .workspaces import (GitWorktreeManager, SharedWorkspaceManager,
                         WorkspaceManager)

__all__ = ["AgentEvent", "Conductor", "GlobalConductor", "MANAGER_TOOLS",
           "Project", "ProjectStore", "ProjectLocator", "ProjectCandidate",
           "ManagerBackend", "FakeManagerBackend", "ManagerTurn", "ToolCall",
           "CodingAgentRuntime",
           "TaskStore", "CorruptStateError", "Task", "Workspace",
           "TASK_STATUSES", "WorkspaceManager", "GitWorktreeManager",
           "SharedWorkspaceManager", "ObservabilityBus", "ObservabilityEvent",
           "JsonlSink", "LoggingSink", "ConsoleSink", "application_log",
           "configure_logging", "current_log_path", "current_run",
           "current_trace", "drain_subprocess_stderr",
           "install_asyncio_exception_handler", "new_run", "new_trace",
           "start_loop_stall_monitor",
           "prune_old_logs"]
