"""CmuxSurface: a Managed Subagent's worker, visible in a cmux workspace.

One workspace per subagent, one terminal surface in it, one real Claude
Code or Codex process inside that. The Conductor still owns identity and
lifecycle; cmux owns pixels and PTYs.

The measured behaviour this is built on, rather than assumed:

Closing a workspace KILLS the provider process. The surface is its host,
which follows from requiring the process to run inside cmux at all. tmux
is no different.

But the provider SESSION outlives it. `claude --resume <id>` in a fresh
workspace brought back a conversation whose process had been killed with
its workspace - so surface loss costs a process, never the work.

That is why create() resumes rather than launches. Attaching to an
execution means attaching to its session; there is no process to reattach
to, and starting a fresh one would be a second worker rather than a view
of the first. A request carrying no provider_session_id is refused for the
same reason: this surface shows existing work, it does not start any.

Synchronous, like the surfaces beside it, so it shells out rather than
sharing the async client.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess

from .cmux_setup import (find_executable, needs_repair, needs_window, repair,
                          unavailable_reason)
from .cmux_runtime import managed_name
from .surfaces import SessionSurface, SurfaceHandle, SurfaceRequest
from .tmux_runtime import session_name

# How the worker is started inside the surface. Always a resume: see above.
RESUME_TEMPLATE = ("env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY "
                   "{claude} --resume {session}")


class CmuxUnavailableError(RuntimeError):
    """cmux cannot be driven. Distinct from a failed command: the caller
    has to choose another surface rather than retry."""


class CmuxSurface(SessionSurface):
    interactive = True

    def __init__(self, binary: str | None = None,
                 claude_binary: str = "claude",
                 password: str | None = None) -> None:
        self.binary = binary or find_executable()
        self.claude = claude_binary
        self._password = password

    # -- plumbing --------------------------------------------------------
    def _env(self) -> dict:
        env = dict(os.environ)
        env["CMUX_QUIET"] = "1"
        secret = self._password or _stored_password()
        if secret:
            env["CMUX_SOCKET_PASSWORD"] = secret
        return env

    # Looking is not a reason to relaunch a cmux the user quit; see the
    # runtime's _REPAIRING for why. Acting on a window is.
    _POLLING = ("workspace", "list-workspaces", "list-windows",
                "list-pane-surfaces")

    def _run(self, *args: str, _repaired: bool = False,
             _windowed: bool = False) -> str:
        if not self.binary:
            raise CmuxUnavailableError("cmux is not installed")
        result = subprocess.run([self.binary, *args], capture_output=True,
                                text=True, env=self._env(), timeout=30)
        text = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            # No window at all (the last tab was closed): nothing can be
            # made until cmux has one. Same rule as the runtime's seam.
            if not _windowed and needs_window(text):
                try:
                    self._run("new-window", _windowed=True)
                except (RuntimeError, CmuxUnavailableError):
                    pass
                return self._run(*args, _repaired=_repaired, _windowed=True)
            # The user closed cmux, or cmux rewrote its config and lost
            # the password. Both are cmux's doing and both are fixable, so
            # fix them and try again rather than making the user restart
            # the product - a click that fails because the window was
            # closed is exactly when they want it to just work.
            if not _repaired and args[0] not in self._POLLING \
                    and needs_repair(text):
                self._password = repair(self.binary) or self._password
                return self._run(*args, _repaired=True)
            gone = unavailable_reason(text)
            if gone:
                raise CmuxUnavailableError(gone)
            raise RuntimeError(text[:300] or f"cmux {args[0]} failed")
        return text

    def _rows(self, text: str) -> list[tuple[str, str, str]]:
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("cmux:"):
                continue
            parts = line.lstrip("* ").split()
            if len(parts) >= 2 and ":" in parts[0]:
                title = " ".join(parts[2:]).replace("[selected]", "").strip()
                out.append((parts[0], parts[1], title))
        return out

    def _workspace_titled(self, title: str) -> tuple[str, str] | None:
        # The JSON listing carries the description, and identity lives
        # there once a workspace is dressed with a human title (see
        # cmux_runtime.managed_name). The text listing only knows titles.
        try:
            listed = json.loads(self._run("workspace", "list", "--json"))
        except (ValueError, RuntimeError, CmuxUnavailableError):
            listed = {}
        for workspace in listed.get("workspaces", []):
            if managed_name(workspace) == title:
                return workspace.get("id", ""), title
        for ref, uuid, name in self._rows(
                self._run("list-workspaces", "--id-format", "both")):
            if name == title:
                return uuid, name
        return None

    def _window_of(self, workspace_id: str) -> str | None:
        """Which cmux window is showing this workspace."""
        for line in self._run("list-windows").splitlines():
            match = re.search(r"(\S+)\s+selected_workspace=(\S+)", line)
            if match and match.group(2) == workspace_id:
                return match.group(1)
        return None

    def _primary_surface(self, workspace_id: str) -> str | None:
        rows = self._rows(self._run("list-pane-surfaces", "--workspace",
                                    workspace_id, "--id-format", "both"))
        return rows[0][1] if rows else None

    # -- the contract ----------------------------------------------------
    def create(self, request: SurfaceRequest) -> SurfaceHandle:
        """Show this execution in cmux, resuming it into a new workspace.

        Never a second workspace for the same subagent: an existing one
        with this title is returned as-is. That is the difference between
        showing the worker again and starting another.
        """
        title = _title_for(request)

        # A worker the cmux RUNTIME is hosting already has a workspace,
        # named for its session rather than for the user. Attaching to it
        # is just finding it - resuming would start a second process on a
        # conversation that already has one.
        hosted = self._workspace_titled(session_name(request.task_id))
        if hosted is not None:
            workspace_id, _ = hosted
            surface_id = self._primary_surface(workspace_id)
            if surface_id:
                return _handle(workspace_id, surface_id, request, title)

        if not request.provider_session_id:
            raise RuntimeError(
                "cmux surfaces show existing work: no provider session to "
                "resume for this execution")

        existing = self._workspace_titled(title)
        if existing is not None:
            workspace_id, _ = existing
            surface_id = self._primary_surface(workspace_id)
            if surface_id:
                return _handle(workspace_id, surface_id, request, title)

        self._run("new-workspace", "--name", title,
                  "--cwd", request.working_directory)
        made = self._workspace_titled(title)
        if made is None:
            raise RuntimeError(f"cmux made no workspace called {title!r}")
        workspace_id, _ = made
        surface_id = self._primary_surface(workspace_id)
        if surface_id is None:
            raise RuntimeError("cmux workspace has no terminal surface")

        command = RESUME_TEMPLATE.format(
            claude=shlex.quote(self.claude),
            session=shlex.quote(request.provider_session_id))
        self._run("send", "--surface", surface_id, command)
        self._run("send-key", "--surface", surface_id, "Enter")
        return _handle(workspace_id, surface_id, request, title)

    def focus(self, handle: SurfaceHandle) -> None:
        """Show this worker: select its workspace and raise cmux.

        All three steps are measured rather than assumed. select-workspace
        picks the right workspace but leaves cmux behind whatever app is
        in front, which reads as the click having done nothing.
        focus-window raises the right window but activated the app only 2
        of 3 tries from the same state - reliable enough to look fixed in
        testing and not reliable enough to ship. `open` activated every
        time but knows nothing of windows. So: select, focus, activate.
        """
        workspace_id = handle.metadata.get("workspace_id", "")
        if not workspace_id:
            return
        self._run("select-workspace", "--workspace", workspace_id)
        window = self._window_of(workspace_id)
        if window:
            self._run("focus-window", "--window", window)
        self._raise_app()

    def _raise_app(self) -> None:
        """Activate cmux. Its own method so it is a seam: nothing else
        here leaves the socket."""
        subprocess.run(["open", "-a", "cmux"], capture_output=True)

    def is_available(self, handle: SurfaceHandle) -> bool:
        """Whether the SURFACE is still there. Says nothing about the
        provider: a closed workspace killed the process, and the session
        behind it is still resumable."""
        workspace_id = handle.metadata.get("workspace_id", "")
        surface_id = handle.metadata.get("surface_id", "")
        try:
            rows = self._rows(self._run("list-pane-surfaces", "--workspace",
                                        workspace_id, "--id-format", "both"))
        except (CmuxUnavailableError, RuntimeError):
            return False
        return any(uuid == surface_id for _, uuid, _ in rows)

    def recover(self, request: SurfaceRequest,
                handle: SurfaceHandle | None = None) -> SurfaceHandle:
        """Rebuild the window around work that is still there.

        Recovery here is a resume, not a reattach - the process died with
        its workspace. Which also means "the process is missing" is the
        normal state after a closed window, not evidence of a lost worker:
        the question that separates recovery from duplication is whether
        the session id still resumes.
        """
        if handle is not None and self.is_available(handle):
            return handle
        return self.create(request)

    def close(self, handle: SurfaceHandle) -> None:
        workspace_id = handle.metadata.get("workspace_id", "")
        if not workspace_id:
            return
        try:
            self._run("close-workspace", "--workspace", workspace_id)
        except (CmuxUnavailableError, RuntimeError):
            pass                      # already gone is not a failure


def _title_for(request: SurfaceRequest) -> str:
    """What the user reads in the cmux sidebar.

    Task identity, not ids: "Posely · Fix login" is recognisable at a
    glance and subagent_82ec7 is not. Identity for US still travels by
    uuid - the title is never used to find a workspace we own, only to
    avoid making a second one with the same name.
    """
    return request.title or f"task {request.task_id}"


def _handle(workspace_id: str, surface_id: str, request: SurfaceRequest,
            title: str) -> SurfaceHandle:
    return SurfaceHandle(
        type="cmux-workspace", application="cmux",
        native_window_id=workspace_id,
        metadata={"workspace_id": workspace_id, "surface_id": surface_id,
                  "workspace_path": request.working_directory,
                  "task_id": request.task_id, "title": title})


def _stored_password() -> str:
    from .cmux_client import PASSWORD_FILE
    try:
        return PASSWORD_FILE.read_text().strip()
    except OSError:
        return ""
