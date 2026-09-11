"""The Manager seam: semantic intelligence in, deterministic execution out.

A ManagerBackend receives one user utterance plus the authoritative task
registry and decides which of the seven Conductor tools to call. The backends:

    FakeManagerBackend    - scripted decisions, for tests (no LLM)
    ClaudeManagerBackend  - a persistent Claude session (claude_manager.py,
                            imported separately so nothing here needs the SDK)

Local state is authoritative; Manager conversational memory is convenience.
Every turn receives fresh task summaries built from state.json.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:                      # avoid a runtime import cycle
    from .conductor import Conductor
    from .task_types import Task


# The voice side attaches the user's own words to a delegation under this
# header (voice_agent._with_verbatim); the block ends at the instruction
# line that follows them. Both halves read the same format.
VERBATIM_HEADER = "[what the user actually said, verbatim, most recent last]"
VERBATIM_FOOTER = "Act on these words."


def users_words(text: str) -> tuple[str, str]:
    """Split a delegation into (frontend summary, the user's own words).

    The words are "" when the text carries no verbatim block - a typed
    turn, or a summary that already was the user's words.
    """
    head, sep, rest = text.partition(VERBATIM_HEADER)
    if not sep:
        return text.strip(), ""
    spoken = rest.split(VERBATIM_FOOTER, 1)[0]
    return head.strip(), " ".join(spoken.split())


@dataclass
class ToolCall:
    tool: str
    args: dict
    result: str = ""


@dataclass
class ManagerTurn:
    """What one user message produced: a spoken/written reply plus the tool
    calls that were executed. No tool calls means the Manager answered
    directly or asked for clarification."""
    reply: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # What the Manager wants the VOICE to know but the user not to hear:
    # ids, files, numbers, caveats, gathered from note_for_voice calls
    # during the turn. Delivered on the commentary channel, never spoken,
    # never typed anywhere.
    commentary: str = ""
    # Read by the Manager together with an earlier message and answered
    # once, on that one: there is nothing to say for this message. A
    # visible Boss does this when words arrive mid-turn (PtyManagerBackend).
    folded: bool = False


def idle_for(task: "Task") -> str:
    """How long since the user last addressed this task, in words.

    Deliberately last_instruction_at rather than updated_at: the latter moves
    on any bookkeeping write - a surface change, a reconcile - so it reports
    a task as fresh that nothing has actually touched in an hour. Recency is
    what separates the task the user just started from three abandoned ones
    sharing its title.
    """
    stamp = task.last_instruction_at or task.created_at
    if not stamp:
        return "unknown"
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    mins = (datetime.now(timezone.utc) - then).total_seconds() / 60
    local = then.astimezone()
    clock = local.strftime("%-I:%M %p").lower()
    if mins < 1:
        return f"just now ({clock})"
    if mins < 60:
        return f"{int(mins)}m ago ({clock})"
    if mins < 24 * 60:
        return f"{int(mins / 60)}h ago ({clock})"
    return f"{int(mins / 1440)}d ago ({local.strftime('%a %-d %b, %-I:%M %p').lower()})"


def clock_block() -> str:
    """Tell the Manager what time it is.

    Every "Last message" is relative, and a model has no reliable sense of
    now, so without an anchor those phrases cannot be checked against
    anything. Stating the current time makes them verifiable.
    """
    local = datetime.now(timezone.utc).astimezone()
    return f"Current time: {local.strftime('%A %-d %B %Y, %-I:%M %p').lower()}"


def task_summary(task: "Task", position: tuple | None = None) -> dict:
    """The compact view of a task the Manager routes with. Deliberately no
    provider session id - models never see those (Invariant 3)."""
    summary = {"task_id": task.id, "title": task.title, "goal": task.goal,
               "status": task.status, "last_message": idle_for(task)}
    if position:
        nth, total = position
        summary["on_screen"] = f"{nth} of {total}"
    return summary


_TERMINAL = ("completed", "failed", "cancelled")

_ORDINALS = ("1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th",
             "9th", "10th")


def stack_positions(tasks: list["Task"]) -> dict:
    """Where each unfinished task sits in the stack the user is looking at.

    The panel renders oldest at the top, newest at the bottom, and the user
    counts from the top - so "the second one" is the second-oldest, which is
    the second-from-LAST row of a newest-first registry.

    Asking the model to invert that itself made things worse, not better:
    told to count backwards it misrouted three ordinals, where before it had
    simply declined to answer. Ordinal arithmetic is not a reasoning task
    worth spending on, so the position is computed here and stated as a
    fact. Finished work has no position; ordinals count what is on screen.
    """
    live = [t for t in tasks if t.status not in _TERMINAL]
    oldest_first = sorted(live,
                          key=lambda t: t.last_instruction_at or t.created_at)
    return {task.id: (index + 1, len(oldest_first))
            for index, task in enumerate(oldest_first)}


def registry_block(tasks: list["Task"]) -> str:
    """Render the live task registry for a Manager prompt."""
    if not tasks:
        return "There are no tasks yet."
    lines = []
    positions = stack_positions(tasks)
    # Newest first: the task the user means is usually the one they just
    # touched, and ordering says so without the model having to compare dates.
    for task in sorted(tasks,
                       key=lambda t: t.last_instruction_at or t.created_at,
                       reverse=True):
        place = ""
        if task.id in positions:
            nth, total = positions[task.id]
            label = _ORDINALS[nth - 1] if nth <= len(_ORDINALS) else f"{nth}th"
            place = (f"  On screen: {label} of {total}"
                     f"{' (the last one)' if nth == total else ''}\n")
        lines.append(f"{task.id}\n  Title: {task.title}\n"
                     f"  Status: {task.status}\n"
                     f"{place}"
                     f"  Last message: {idle_for(task)}\n"
                     f"  Goal: {task.goal}")
    return "\n".join(lines)


class ManagerBackend(ABC):
    @abstractmethod
    async def handle(self, text: str, conductor: "Conductor") -> ManagerTurn:
        """Interpret one user message and drive conductor.handle_action()."""

    @property
    def busy(self) -> bool:
        """Whether a turn is in flight right now, so a caller can say that a
        new message is queued behind it rather than being worked on."""
        return False


class FakeManagerBackend(ManagerBackend):
    """Scripted decisions: tests force the next actions, then verify the
    Conductor executed them correctly - `Manager decision -> execution`
    tested separately from `natural language -> Manager decision`."""

    def __init__(self) -> None:
        self.next_actions: list[tuple[str, dict]] = []
        self.next_reply: str = "ok"
        self.seen: list[str] = []

    async def handle(self, text: str, conductor: "Conductor") -> ManagerTurn:
        self.seen.append(text)
        turn = ManagerTurn(reply=self.next_reply)
        actions, self.next_actions = self.next_actions, []
        for tool, args in actions:
            result = await conductor.handle_action(tool, args)
            turn.tool_calls.append(ToolCall(tool=tool, args=args,
                                            result=str(result)[:200]))
        return turn
