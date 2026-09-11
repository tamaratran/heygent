"""Session surfaces: how a managed subagent's execution is exposed to the
user.

Task != provider session != surface. The task is the durable goal, the
managed subagent's provider session is the one worker executing it, and a
surface ATTACHES to that existing execution - it never forks it, never
creates another agent, and is never the source of task state (state flows
from structured runtime events).

Implementations, by preference (SurfacePreference):

    interactive-terminal   PTY-attached live session - input reaches the
                           worker (future; requires an attachable execution)
    claude-app/codex-app   provider app deep-link to the same session
                           (future; needs reliable provider support)
    transcript             TranscriptSurface: a Terminal.app window
                           live-tailing the execution's own event stream.
                           Read-only, and labelled as such - today's
                           implementation and the universal fallback.
"""

from __future__ import annotations

import secrets
import shlex
import subprocess
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

from .observability import application_log

SURFACE_TYPES = ("terminal-window", "terminal-tab", "claude-app",
                 "codex-app", "vscode-terminal", "warp", "custom", "fake")


@dataclass
class SurfaceHandle:
    type: str
    id: str = field(default_factory=lambda: "surf_" + secrets.token_hex(4))
    application: str = ""
    native_window_id: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SurfaceHandle":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__
                      if k in data})


@dataclass
class SurfaceRequest:
    project_id: str
    task_id: str
    title: str
    working_directory: str
    provider: str
    provider_session_id: str | None = None
    transcript_path: str | None = None    # the execution's live stream
    pty_handle: str | None = None         # attachable PTY, when one exists


class SessionSurface(ABC):
    """How a managed subagent's EXISTING execution is exposed to the user.

    The invariant: a surface attaches to the execution; creating or focusing
    one must never fork, resume-as-copy, or create another coding agent.

    interactive says whether input through this surface reaches the worker.
    A read-only implementation must say so and look so - it must never
    pretend to be the live session prompt.
    """

    interactive: bool = False

    @abstractmethod
    def create(self, request: SurfaceRequest) -> SurfaceHandle:
        """Attach to the execution described by the request."""

    # attach IS create: the name exists so call sites can say what they mean.
    def attach(self, request: SurfaceRequest) -> SurfaceHandle:
        return self.create(request)

    @abstractmethod
    def focus(self, handle: SurfaceHandle) -> None: ...

    @abstractmethod
    def is_available(self, handle: SurfaceHandle) -> bool: ...

    def close(self, handle: SurfaceHandle) -> None:
        pass                          # optional per implementation


@dataclass
class SurfacePreference:
    """Which surface implementation each provider's subagents prefer, in
    order, with the read-only transcript as the universal fallback. The
    Conductor picks the first *registered and working* implementation;
    nothing here can create a second worker, only a different view.

    Future implementations slot in behind the same names:
        interactive-terminal   PTY-attached live session (input reaches it)
        claude-app / codex-app provider app deep-link to the same session
        transcript             read-only stream of the execution (today)
    """
    # No "transcript" fallback: a read-only tail window is not a session the
    # user can use, and offering one as a last resort meant a failed
    # attachment looked like a successful one.
    claude_code: tuple = ("interactive-terminal", "claude-app")
    codex: tuple = ("codex-app", "interactive-terminal")

    def order_for(self, provider: str) -> tuple:
        return self.claude_code if provider == "claude-code" else self.codex


class FakeSurface(SessionSurface):
    """Records surface operations for tests; windows are dict entries."""

    def __init__(self) -> None:
        self.created: list[SurfaceRequest] = []
        self.focused: list[str] = []
        self.open: dict[str, SurfaceRequest] = {}

    def create(self, request: SurfaceRequest) -> SurfaceHandle:
        handle = SurfaceHandle(type="fake",
                               metadata={"task_id": request.task_id})
        self.created.append(request)
        self.open[handle.id] = request
        return handle

    def focus(self, handle: SurfaceHandle) -> None:
        if handle.id not in self.open:
            raise RuntimeError("surface is gone")
        self.focused.append(handle.id)

    def is_available(self, handle: SurfaceHandle) -> bool:
        return handle.id in self.open

    def close(self, handle: SurfaceHandle) -> None:
        self.open.pop(handle.id, None)


def _osascript(script: str) -> str:
    result = subprocess.run(["osascript", "-e", script],
                            capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"osascript: {result.stderr.strip()}")
    return result.stdout.strip()


class TranscriptSurface(SessionSurface):
    """READ-ONLY view: a Terminal.app window live-tailing the execution's
    transcript - the stream the runtime writes from the very session doing
    the work. What the user watches is the actual execution (prompt, tool
    calls, replies, approvals, completion, same session id in the header),
    but typing here reaches nothing: it says so in the banner, and steering
    happens through voice/the Manager.

    This is deliberately not called an interactive session surface, because
    it is not one. It is the universal fallback and debug view; interactive
    attachment (a PTY-hosted execution) is a different implementation behind
    the same SessionSurface interface."""

    interactive = False
    READ_ONLY_BANNER = ("READ-ONLY VIEW of the live agent - typing here "
                        "does nothing; speak to the conductor to steer it.")

    def command_for(self, request: SurfaceRequest) -> str:
        """The shell command the window runs; split out for testability."""
        header = f"{request.title}\n{self.READ_ONLY_BANNER}\n"
        show = f"tail -n +1 -f {shlex.quote(request.transcript_path)}"
        return (f"cd {shlex.quote(request.working_directory)} && "
                f"clear && printf {shlex.quote(header)} && {show}")

    def create(self, request: SurfaceRequest) -> SurfaceHandle:
        if not request.transcript_path:
            raise RuntimeError("TranscriptSurface needs an execution "
                               "transcript; this runtime provides none")
        command = self.command_for(request)
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        window_id = _osascript(
            'tell application "Terminal"\n'
            f'  set t to do script "{escaped}"\n'
            f'  set custom title of t to "{request.title}"\n'
            "  activate\n"
            "  return id of front window\n"
            "end tell")
        return SurfaceHandle(type="terminal-window",
                             application="Terminal.app",
                             native_window_id=window_id,
                             metadata={"task_id": request.task_id,
                                       "title": request.title})

    def focus(self, handle: SurfaceHandle) -> None:
        _osascript(
            'tell application "Terminal"\n'
            f'  set index of window id {handle.native_window_id} to 1\n'
            "  activate\n"
            "end tell")

    def is_available(self, handle: SurfaceHandle) -> bool:
        try:
            ids = _osascript('tell application "Terminal" to '
                             'return id of every window')
        except RuntimeError:
            return False
        return handle.native_window_id in [
            part.strip() for part in ids.split(",")]

    def close(self, handle: SurfaceHandle) -> None:
        try:
            _osascript('tell application "Terminal" to '
                       f'close window id {handle.native_window_id}')
        except RuntimeError:
            # Already gone is the desired state, but say which window and why.
            application_log("ui", "surface.close_failed",
                            "could not close the transcript window",
                            severity="debug", exc_info=True,
                            window=handle.native_window_id)


# Backward-compatible name; the honest one is TranscriptSurface.
TerminalSurface = TranscriptSurface


class InteractiveTerminalSurface(SessionSurface):
    """A Terminal.app window attached to the execution's own PTY (tmux).

    Fully interactive: what the user sees IS the live claude process, and
    what they type goes straight into it - the Conductor keeps supervising
    the same session through its structured event stream. Attach never
    forks: `tmux attach` joins the one existing process. Requires a runtime
    that hosts executions in a PTY (TmuxClaudeRuntime) and exposes the
    handle via SurfaceRequest.pty_handle."""

    interactive = True

    def command_for(self, request: SurfaceRequest) -> str:
        return f"tmux attach-session -t {shlex.quote(request.pty_handle)}"

    def create(self, request: SurfaceRequest) -> SurfaceHandle:
        if not request.pty_handle:
            raise RuntimeError("no attachable PTY for this execution; use "
                               "the transcript surface instead")
        command = self.command_for(request)
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        window_id = _osascript(
            'tell application "Terminal"\n'
            f'  set t to do script "{escaped}"\n'
            f'  set custom title of t to "{request.title}"\n'
            "  activate\n"
            "  return id of front window\n"
            "end tell")
        return SurfaceHandle(type="terminal-window",
                             application="Terminal.app",
                             native_window_id=window_id,
                             metadata={"task_id": request.task_id,
                                       "pty": request.pty_handle,
                                       "title": request.title})

    def focus(self, handle: SurfaceHandle) -> None:
        _osascript(
            'tell application "Terminal"\n'
            f'  set index of window id {handle.native_window_id} to 1\n'
            "  activate\n"
            "end tell")

    def is_available(self, handle: SurfaceHandle) -> bool:
        try:
            ids = _osascript('tell application "Terminal" to '
                             'return id of every window')
        except RuntimeError:
            return False
        return handle.native_window_id in [
            part.strip() for part in ids.split(",")]

    def close(self, handle: SurfaceHandle) -> None:
        try:
            _osascript('tell application "Terminal" to '
                       f'close window id {handle.native_window_id}')
        except RuntimeError:
            application_log("ui", "surface.close_failed",
                            "could not close the interactive window",
                            severity="debug", exc_info=True,
                            window=handle.native_window_id)
