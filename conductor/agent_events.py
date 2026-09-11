"""Normalized worker events.

Provider-specific events (Claude's message stream, later Codex's) are
translated into these; everything above the provider adapters consumes only
AgentEvent. That is what keeps the Conductor provider-agnostic.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

EVENT_TYPES = ("started", "progress", "needs_input", "approval_required",
               "approval_resolved", "checkpoint", "completed", "failed")

# How much of a worker's final message a completed event carries: all of
# it, short of a worker pasting a file. The message goes to the Boss
# whole - what the USER sees of it (a notification body, a card's one
# line, a spoken sentence) is cut where it is shown. Measured
# 2026-08-30 23:41:59Z: a 980-character finish said "Fix: PR #110" at
# character 470, after a 600-character cut had already been applied to
# the turn's earlier prose, and the Boss never heard of the PR.
SUMMARY_CEILING = 8000


def keep_end(text: str, limit: int) -> str:
    """text, or its last `limit` characters when it is longer - starting
    at a sentence if one begins early enough, else at a word - with an
    ellipsis for what was cut. A finish is read for its outcome, and
    the outcome (the PR, the number, the next step) is where a message
    ENDS; a cut that keeps the beginning keeps the preamble."""
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    starts = [tail.find(mark) for mark in (". ", "! ", "? ")]
    starts = [i for i in starts if i >= 0]
    cut = min(starts) + 2 if starts else -1
    if 0 <= cut <= limit // 3:
        return "…" + tail[cut:]
    space = tail.find(" ")
    return "…" + (tail[space + 1:] if 0 <= space <= limit // 3 else tail)

# The provider-neutral vocabulary above, in the supervision taxonomy the
# rest of the product reasons in. A provider adapter says "progress"; the
# reducer and the inbox ask "what kind of thing was that?".
KIND_OF = {
    "started": "session_started",
    "progress": "activity_changed",
    "checkpoint": "milestone",
    "approval_required": "approval_required",
    "approval_resolved": "approval_resolved",
    "needs_input": "input_required",
    "completed": "completed",
    "failed": "failed",
}


@dataclass(frozen=True)
class AgentEvent:
    type: str
    summary: str = ""
    question: str = ""           # needs_input
    error: str = ""              # failed
    detail: dict = field(default_factory=dict)   # provider extras, debug only
    # Durable identity, so a replayed event is the same event: applying it
    # twice changes nothing. Assigned at creation; a provider that has its
    # own id passes it through.
    event_id: str = field(default_factory=lambda: "aev_" + secrets.token_hex(6))
    # Position within one execution, stamped by the runtime as it emits.
    # 0 means unstamped. A lower sequence than one already applied is
    # stale and must not rewind state - a late "running tests" cannot pull
    # a finished worker back to Working.
    sequence: int = 0
    # Which counting the sequence belongs to. A runtime process counts
    # from 1; the reducer persists the high-water mark. Measured: after
    # every restart of the app the next N events of every live worker
    # were dropped as stale (61 for one worker), because a fresh process
    # started at 1 against a persisted 45. An event from a different
    # epoch starts a new sequence space. Empty means "unstamped" and is
    # compared as before.
    epoch: str = ""
    # The whole of a prose line, where summary is cut for cards and logs.
    # For a viewer drawing the reply as it is written; never recorded.
    text: str = ""

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown agent event type: {self.type!r}")

    @property
    def kind(self) -> str:
        return KIND_OF[self.type]
