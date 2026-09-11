"""The common coding-agent interface every provider adapter implements.

Nothing outside a provider adapter may depend on Claude-specific (or later
Codex-specific) session APIs. If adding a provider requires changing code
above this interface, the abstraction is leaking.
"""

from __future__ import annotations

import re

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .agent_events import AgentEvent
from .observability import application_log


@dataclass
class TaskExecution:
    """The one execution behind a task: the exact provider session doing the
    work. Every surface, follow-up, approval and completion targets this -
    a second execution for the same task is an invariant violation."""
    task_id: str
    provider: str
    provider_session_id: str
    workspace_path: str
    status: str = "running"
    transcript_path: str | None = None
    # Attachability, when the provider/runtime can host the execution in a
    # shareable PTY or expose an app deep-link: what interactive surfaces
    # attach to. None means only the read-only transcript view is possible.
    pty_handle: str | None = None
    attach_token: str | None = None


class ExecutionTranscript:
    """The live, human-readable stream of ONE execution, written by the
    runtime from the same structured events that drive task state.

    This is what a visible terminal renders: the prompt going in, every tool
    call, every reply, approvals, and completion - of the exact session the
    Conductor controls (its id is in the header, so a mismatch between what
    the user watches and what the Conductor drives is immediately visible).
    Not scraping: the events are the source; the terminal is a faithful tail
    of them.
    """

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.log"

    def write(self, task_id: str, text: str) -> None:
        try:
            with self.path(task_id).open("a") as handle:
                handle.write(text.rstrip("\n") + "\n")
        except OSError:
            application_log("runtime", "transcript.write_failed",
                            f"could not write transcript for {task_id}",
                            severity="warning", exc_info=True,
                            task_id=task_id, path=str(self.path(task_id)))

    def header(self, task_id: str, title: str, session_id: str,
               workspace: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.write(task_id, f"{'=' * 60}\n{title}\n"
                            f"session {session_id}\n"
                            f"workspace {workspace}\n"
                            f"started {stamp}\n{'=' * 60}")


def _bash_command(description: str) -> str:
    """The command out of a permission prompt, whatever shape it arrives in.

    Providers word these differently. Three shapes are in use:

        Bash(git status) | Check the tree          the SDK gist
        pytest -q | Run the suite | Needs approval the TUI prompt
        Bash command | pytest -q | Run the suite   a bare tool label

    Treating any segment beginning with "bash" as boilerplate discarded the
    command in the first shape and classified the human-readable description
    instead, so every command asked - `git status` and `ls` included - no
    matter what the safe list said.
    """
    text = " ".join(str(description).split())
    match = re.search(r"(?is)\bbash\s*\((.*)\)", text)
    if match:
        return match.group(1).strip()
    labels = {"bash", "bash command", "command", "shell", "run command"}
    for part in text.split("|"):
        part = part.strip().lstrip("$").strip()
        if not part or part.lower().rstrip(":") in labels:
            continue                     # a tool label, not the command
        if part.lower().startswith(("do you", "this command", "command:")):
            continue
        return part
    return text


@dataclass
class ApprovalPolicy:
    """Provider-independent approval policy (spec section 20): everyday
    reads and edits flow, risk asks. Bash is classified by command prefix."""
    safe_bash_prefixes: tuple = (
        "ls", "cat", "pwd", "echo", "which", "head", "tail", "wc",
        "git status", "git diff", "git log", "git show", "git branch",
        "pytest", "python -m pytest", "python3 -m pytest", "npm test",
        "pnpm test", "yarn test", "npm run build", "make test", "tox",
        "python -m unittest", "python3 -m unittest")
    always_ask_markers: tuple = (
        "rm -rf", "git push", "git merge", "sudo", "drop table",
        "npm publish", "pip install", "npm install", "pnpm install",
        "brew install", "curl", "wget")

    def decide(self, tool_name: str, args: dict) -> str:
        """"allow" or "ask"."""
        if tool_name != "Bash":
            return "allow"           # non-Bash risk is fenced by tool lists
        command = " ".join(str(args.get("command", "")).split()).lower()
        verdict = self._computer(command)
        if verdict is not None:
            return verdict
        if any(marker in command for marker in self.always_ask_markers):
            return "ask"
        if any(command.startswith(prefix)
               for prefix in self.safe_bash_prefixes):
            return "allow"
        return "ask"                 # unknown commands ask, never assume

    # The computer driver's read-only verbs. Looking at the screen and
    # listing what is running are reads; anything that opens an app, moves
    # the cursor or presses a key acts on the user's machine and asks,
    # every time.
    _COMPUTER_SAFE_VERBS = ("look", "check", "apps")

    def _computer(self, command: str) -> str | None:
        """Classify a computer-driver invocation, or None if it is not
        one. Matched by the script path so it holds wherever the repo
        lives and whatever the worker's cwd is."""
        marker = "conductor/computer.py"
        index = command.find(marker)
        if index < 0:
            return None
        rest = command[index + len(marker):].strip()
        verb = rest.split(" ", 1)[0] if rest else ""
        return "allow" if verb in self._COMPUTER_SAFE_VERBS else "ask"

    # Actions no policy may ever auto-approve; the supervisor denies them
    # outright rather than leaving a worker waiting on something the user
    # should never be asked to rubber-stamp.
    never_allow_markers: tuple = ("rm -rf /", "rm -rf ~", "force push",
                                  "push --force", "push -f",
                                  "drop database", "mkfs", ":(){",
                                  "> /dev/sda")

    def supervise(self, description: str, allow_write: bool = True) -> str:
        """The supervisor's three-way call on a structured approval:
        "allow", "deny", or "ask". Forbidden actions deny; routine ones
        allow; genuinely consequential ones escalate to the user."""
        lower = " ".join(str(description).lower().split())
        if any(marker in lower for marker in self.never_allow_markers):
            return "deny"
        return self.decide_prompt(description, allow_write)

    # Prompt-kind keywords for TUI permission gates, where all we have is
    # the prompt text. Routine development reads never interrupt the user.
    _READ_KINDS = ("read", "search", "grep", "glob", "list", "fetch file",
                   "view")
    _WRITE_KINDS = ("edit", "write", "create file", "multiedit",
                    "notebookedit")

    def decide_prompt(self, description: str,
                      allow_write: bool = False) -> str:
        """Classify a provider permission PROMPT (interactive TUI) where the
        tool kind must be parsed from text. Repo reads and searches are
        routine and auto-allowed; ordinary workspace edits follow the
        write policy; Bash commands are classified as commands; anything
        unrecognized asks - never assumes."""
        lower = " ".join(description.lower().split())
        head = lower.split("|")[0]
        if "bash" in head or lower.startswith("$"):
            return self.decide("Bash", {"command": _bash_command(description)})
        if any(kind in head for kind in ("web", "network", "http", "url")):
            return "ask"             # anything leaving the machine asks
        if any(kind in head for kind in self._READ_KINDS):
            return "allow"
        if any(kind in head for kind in self._WRITE_KINDS):
            return "allow" if allow_write else "ask"
        return "ask"

# What get_status may return. "idle" means the session is alive but between
# turns; "disconnected" means the process is gone and needs resume().
AGENT_STATUSES = ("starting", "running", "idle", "disconnected")

# What reconcile_session may return: the full health picture. "unreachable"
# is temporary uncertainty and is NOT "missing" - missing means positively
# determined unrecoverable, and only recovery may decide that.
PROVIDER_HEALTH = ("starting", "running", "idle", "waiting_for_input",
                   "waiting_for_approval", "interrupted", "completed",
                   "failed", "unreachable", "missing")

EventHandler = Callable[[AgentEvent], None]


class CodingAgentRuntime(ABC):
    # Set by implementations that stream a visible execution transcript.
    transcript: ExecutionTranscript | None = None

    @abstractmethod
    async def create_session(self, task_id: str, working_directory: str,
                             initial_prompt: str) -> str:
        """Start a new provider session; returns the provider session id.
        MUST refuse to create a second live session for the same task: one
        task, one execution."""

    async def executions(self) -> list[TaskExecution]:
        """The live executions this runtime is driving, for invariant checks
        and debugging (task -> session -> workspace -> transcript)."""
        return []

    @abstractmethod
    async def send(self, session_id: str, message: str) -> None:
        """Queue a follow-up message into a live session."""

    @abstractmethod
    async def interrupt(self, session_id: str) -> None:
        """Stop the session's current work; the session stays resumable."""

    @abstractmethod
    async def resume(self, session_id: str,
                     working_directory: str | None = None) -> None:
        """Reconnect a session that is not currently live (e.g. after a
        restart). working_directory comes from the task's workspace record
        because a dead runtime no longer knows it."""

    @abstractmethod
    async def get_status(self, session_id: str) -> str:
        """One of AGENT_STATUSES."""

    async def reconcile_session(self, session_id: str) -> str:
        """One of PROVIDER_HEALTH. The answer to "is this worker actually
        stuck?" - silence must trigger this, never a replacement. The default
        maps get_status conservatively: a session this runtime cannot see is
        unreachable, not missing."""
        status = await self.get_status(session_id)
        return {"starting": "starting", "running": "running", "idle": "idle",
                "disconnected": "unreachable"}.get(status, "unreachable")

    async def pending_approvals(self, session_id: str) -> list[dict]:
        """Structured approvals awaiting a decision, if the provider
        supports them."""
        return []

    async def resolve_approval(self, session_id: str, approval_id: str,
                               approve: bool) -> None:
        """Answer one pending approval. Must be idempotent-safe: resolving
        an unknown or already-resolved approval raises rather than blindly
        approving whatever is current."""
        raise KeyError(f"no pending approval {approval_id}")

    @abstractmethod
    async def subscribe(self, session_id: str,
                        handler: EventHandler) -> Callable[[], None]:
        """Register a handler for this session's AgentEvents; returns an
        unsubscribe function."""

    @abstractmethod
    async def destroy(self, session_id: str) -> None:
        """Disconnect and forget the session."""
