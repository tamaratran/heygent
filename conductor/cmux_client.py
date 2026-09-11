"""A typed client for cmux's control socket.

cmux is a native macOS terminal built for coding agents: vertical tabs,
workspaces in a sidebar, and a Unix socket that can drive all of it. The
spec wants it as the visible surface for Managed Subagents - the place the
real Claude Code or Codex process runs - while the Conductor stays the
source of truth for identity, lifecycle and open-session state.

Every call here was exercised against cmux 0.64.22 before it was written.
The method names follow the CLI rather than an invented protocol, because
the last two times I designed against an API I had not run, both designs
were wrong.

Two facts the socket makes you confront immediately:

Access is denied by default. cmux ships socketControlMode "cmuxOnly", so
only processes started inside cmux may connect - a Conductor running
outside it cannot. The deliberate setting is "password" with a
socketPassword, which grants us access without opening control to every
local process the way allowAll would.

And the socket only exists while the app runs. There is no daemon to talk
to when cmux is closed, so "is cmux available" is a real question with a
real answer, not a formality.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .cmux_setup import unavailable_reason

SOCKET = Path.home() / ".local/state/cmux/cmux.sock"
PASSWORD_FILE = Path.home() / ".config/cmux/.voice-agent-socket-password"


@dataclass(frozen=True)
class CmuxWorkspace:
    id: str                  # stable UUID, never the title
    ref: str                 # workspace:2 - convenient, NOT stable
    title: str
    selected: bool = False


@dataclass(frozen=True)
class CmuxSurface:
    id: str
    ref: str
    title: str
    selected: bool = False


class CmuxUnavailable(RuntimeError):
    """cmux cannot be reached: not installed, not running, or not
    permitting us. Deliberately distinct from a command failing - the
    caller has to choose a different surface rather than retry."""


class CmuxClient:
    def __init__(self, binary: str | None = None,
                 password: str | None = None) -> None:
        self.binary = binary or shutil.which("cmux") or "cmux"
        self._password = password

    # -- plumbing --------------------------------------------------------
    def password(self) -> str:
        if self._password is not None:
            return self._password
        from_env = os.environ.get("CMUX_SOCKET_PASSWORD")
        if from_env:
            return from_env
        try:
            return PASSWORD_FILE.read_text().strip()
        except OSError:
            return ""

    def _env(self) -> dict:
        env = dict(os.environ)
        password = self.password()
        if password:
            env["CMUX_SOCKET_PASSWORD"] = password
        env["CMUX_QUIET"] = "1"       # silence the alias deprecation notes
        return env

    async def _run(self, *args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, *args, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self._env())
            out, err = await proc.communicate()
        except OSError as exc:
            raise CmuxUnavailable(f"cannot run cmux: {exc}") from exc
        text = (out.decode() + err.decode()).strip()
        if proc.returncode != 0:
            gone = unavailable_reason(text)
            if gone:
                raise CmuxUnavailable(gone)
            raise RuntimeError(text[:300] or f"cmux {args[0]} failed")
        return text

    # -- availability ----------------------------------------------------
    async def ping(self) -> bool:
        """Whether cmux is installed, running, and permitting us. All three
        fail differently and all three mean the same thing to a caller."""
        try:
            return "PONG" in await self._run("ping")
        except (CmuxUnavailable, RuntimeError):
            return False

    async def capabilities(self) -> dict:
        try:
            return json.loads(await self._run("capabilities"))
        except (CmuxUnavailable, RuntimeError, json.JSONDecodeError):
            return {}

    # -- workspaces ------------------------------------------------------
    async def create_workspace(self, name: str, cwd: str) -> CmuxWorkspace:
        """One workspace per Managed Subagent. The name is what the user
        reads in the sidebar, so it carries task identity - the id is what
        we store."""
        await self._run("new-workspace", "--name", name, "--cwd", str(cwd))
        for workspace in await self.list_workspaces():
            if workspace.title == name:
                return workspace
        raise RuntimeError(f"cmux made no workspace called {name!r}")

    async def list_workspaces(self) -> list[CmuxWorkspace]:
        return _workspaces(await self._run("list-workspaces",
                                           "--id-format", "both"))

    async def focus_workspace(self, workspace_id: str) -> None:
        await self._run("select-workspace", "--workspace", workspace_id)

    async def close_workspace(self, workspace_id: str) -> None:
        await self._run("close-workspace", "--workspace", workspace_id)

    async def rename_workspace(self, workspace_id: str, title: str) -> None:
        await self._run("rename-workspace", "--workspace", workspace_id,
                        title)

    # -- surfaces --------------------------------------------------------
    async def list_surfaces(self, workspace_id: str) -> list[CmuxSurface]:
        return _surfaces(await self._run("list-pane-surfaces", "--workspace",
                                         workspace_id, "--id-format", "both"))

    async def primary_surface(self, workspace_id: str) -> CmuxSurface | None:
        surfaces = await self.list_surfaces(workspace_id)
        return surfaces[0] if surfaces else None

    async def focus_surface(self, surface_id: str) -> None:
        await self._run("focus-pane", "--pane", surface_id)

    async def send(self, surface_id: str, text: str,
                   enter: bool = True) -> None:
        """Type into one surface. Addressed by id, never by "whichever is
        focused" - that is how a follow-up reaches the wrong worker."""
        await self._run("send", "--surface", surface_id, text)
        if enter:
            await self._run("send-key", "--surface", surface_id, "Enter")

    async def send_key(self, surface_id: str, key: str) -> None:
        await self._run("send-key", "--surface", surface_id, key)

    async def read_screen(self, surface_id: str, lines: int = 40,
                          scrollback: bool = True) -> str:
        args = ["read-screen", "--surface", surface_id, "--lines", str(lines)]
        if scrollback:
            args.append("--scrollback")
        try:
            return await self._run(*args)
        except (CmuxUnavailable, RuntimeError):
            return ""

    async def surface_exists(self, workspace_id: str,
                             surface_id: str) -> bool:
        """Surface health, which is NOT provider health. A closed surface
        says nothing about whether the worker behind it is alive."""
        try:
            return any(s.id == surface_id
                       for s in await self.list_surfaces(workspace_id))
        except (CmuxUnavailable, RuntimeError):
            return False


def _rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("cmux:"):
            continue
        selected = line.startswith("*")
        parts = line.lstrip("* ").split()
        if len(parts) >= 2 and ":" in parts[0]:
            rows.append([str(selected), *parts])
    return rows


def _workspaces(text: str) -> list[CmuxWorkspace]:
    out = []
    for selected, ref, uuid, *rest in _rows(text):
        out.append(CmuxWorkspace(id=uuid, ref=ref,
                                 title=" ".join(rest).replace("[selected]", "").strip(),
                                 selected=selected == "True"))
    return out


def _surfaces(text: str) -> list[CmuxSurface]:
    out = []
    for selected, ref, uuid, *rest in _rows(text):
        out.append(CmuxSurface(id=uuid, ref=ref,
                               title=" ".join(rest).replace("[selected]", "").strip(),
                               selected=selected == "True"))
    return out
