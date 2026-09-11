"""What "open" means.

A provider session that still exists is not an open session. Neither is an
idle one, nor a finished one whose transcript happens to be resumable. Open
describes what the user currently has in front of them - and the Manager has
to share that reading, or it answers "how many agents do I have open?" with a
number from the filesystem.

Three dimensions, deliberately not collapsed into one flag:

    execution   what the worker is doing        working, completed, ...
    display     whether the user can see it     visible_active, visible_recent, hidden
    lifecycle   where it sits in history        current, recent, retired

    open = display is visible_active and execution is not terminal

The provider's own state never appears in that rule. "idle" in particular
says only that no tokens are being generated right now; a worker waiting for
you and a worker that finished yesterday are both idle, and they are not the
same thing.
"""

from __future__ import annotations

from dataclasses import dataclass

# Execution: what the worker is doing.
TERMINAL = ("completed", "failed", "cancelled")
NON_TERMINAL = ("queued", "starting", "running", "waiting_for_user",
                "waiting_for_approval", "waiting_for_input", "paused",
                "interrupted")

# Display: what the user can see.
VISIBLE_ACTIVE = "visible_active"
VISIBLE_RECENT = "visible_recent"
HIDDEN = "hidden"
DISPLAY_STATES = (VISIBLE_ACTIVE, VISIBLE_RECENT, HIDDEN)

# Lifecycle: where it sits in history.
CURRENT = "current"
RECENT = "recent"
RETIRED = "retired"
LIFECYCLES = (CURRENT, RECENT, RETIRED)

# How long a finished session stays on screen as a recent card.
RECENT_RETENTION_S = 15 * 60


def is_terminal(execution: str) -> bool:
    return execution in TERMINAL


@dataclass
class Presence:
    """One session's place in the user's world."""
    execution: str
    display: str = VISIBLE_ACTIVE
    lifecycle: str = CURRENT
    resumable: bool = False

    @property
    def is_open(self) -> bool:
        """The whole product definition, in one line."""
        return self.display == VISIBLE_ACTIVE and not is_terminal(self.execution)

    @property
    def is_visible(self) -> bool:
        return self.display in (VISIBLE_ACTIVE, VISIBLE_RECENT)


def classify(execution: str, *, hidden_by_user: bool = False,
             seconds_since_finished: float | None = None,
             has_provider_session: bool = False) -> Presence:
    """Derive presence from state the Conductor owns.

    Deterministic on purpose: whether a session is retired is not a judgement
    call, and asking a model to feel it out is how the roster drifts from
    what the user sees.
    """
    resumable = has_provider_session
    if hidden_by_user:
        # Hiding a card removes it from the open roster. It does not stop the
        # worker: a hidden session that is still running is real, and gets
        # described as such rather than counted as open.
        return Presence(execution, HIDDEN,
                        RETIRED if is_terminal(execution) else CURRENT,
                        resumable)
    if not is_terminal(execution):
        return Presence(execution, VISIBLE_ACTIVE, CURRENT, resumable)
    # Finished: visible for a while as a recent card, then out of the tray.
    if (seconds_since_finished is not None
            and seconds_since_finished > RECENT_RETENTION_S):
        return Presence(execution, HIDDEN, RETIRED, resumable)
    return Presence(execution, VISIBLE_RECENT, RECENT, resumable)


def roster(presences: list[Presence]) -> dict:
    """The counts the Manager and the tray both answer from."""
    return {
        "open": sum(1 for p in presences if p.is_open),
        "visible_cards": sum(1 for p in presences if p.is_visible),
        "hidden_running": sum(1 for p in presences
                              if p.display == HIDDEN
                              and not is_terminal(p.execution)),
        "resumable": sum(1 for p in presences if p.resumable),
    }
