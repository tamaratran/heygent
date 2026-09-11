"""SessionCardProjector: canonical state in, exact card out.

    SubagentState -> project_card() -> the one card for this worker

Deterministic and total: every status has a card, no status consults a
model, a notification record, or whether anything was spoken. The card
was previously built from the latest TaskNotification, which meant the
live tray depended on the notification pipeline being up and having
fired - a progress tick that produced no notification produced no card
change, and a NotificationService hiccup left a finished worker showing
"Working". Now the card follows the state, and only the state.

One card per Managed Subagent, keyed on the task id: a state change
mutates the existing card. Events belong in history; cards are sessions.
"""

from __future__ import annotations

from .agent_activity import label
from .notifications import concise
from .plain_text import plain_text

# What the overlay draws. Kept to four so the renderer stays a projection:
#   working    spinner
#   done       tick
#   attention  the worker is waiting on the user
#   failed     the worker stopped in a bad way
GLYPHS = ("working", "done", "attention", "failed")

ATTENTION_STATES = ("waiting_for_approval", "waiting_for_input")
FINISHED_STATES = ("completed", "cancelled")


def project_card(state: dict) -> dict:
    """The card for one worker, from its canonical state dict."""
    status = state.get("status") or "starting"
    text = status_text(state)
    glyph = _glyph(state)
    requires_attention = status in ATTENTION_STATES or status == "failed"
    dismissed_at = int(state.get("dismissed_at_revision") or 0)
    dismissed = dismissed_at > 0
    # News is a result or a question that arrived after the dismissal.
    news = max(int(state.get("result_revision") or 0),
               int(state.get("attention_revision") or 0)) > dismissed_at
    force = (requires_attention or glyph == "done") and news
    return {
        "id": state["task_id"],
        "task_id": state["task_id"],
        "subagent_id": state.get("id") or f"sub_{state['task_id']}",
        "title": state.get("title") or state["task_id"],
        "state": status,
        "body": concise(text, 160),
        "status_text": text,
        "requires_attention": requires_attention,
        # Legacy glyph key the overlay already reads: done or working.
        # `glyph` carries the finer distinction for renderers that have it.
        "status": "done" if glyph == "done" else "working",
        "glyph": glyph,
        # A question or a finish reopens a card the user dismissed - but
        # only one that arrived AFTER the dismissal. Plain progress never
        # does, and neither does re-projecting the same finish: dismissal
        # means "seen up to here", not "never again" and not "until the
        # next tick".
        "force": force,
        # Still dismissed: the renderer keeps (or puts) it away. This is
        # how a dismissal survives a restart - the projection says so,
        # rather than a set in the overlay's memory.
        "dismissed": dismissed and not force,
    }


def status_text(state: dict) -> str:
    status = state.get("status") or "starting"
    result = state.get("result") or {}
    summary = _one_line(result.get("summary", ""))
    approval = (state.get("pending_approval") or {}).get("question", "")
    question = (state.get("pending_input") or {}).get("question", "")
    if status == "starting":
        return "Starting…"
    if status == "working":
        activity = state.get("activity") or ""
        if activity:
            return label(activity, state.get("activity_text") or "")
        return "Working…"
    if status == "idle":
        # A turn ended and the worker is standing by. With a result it
        # has answered - which is not the same as the task being done:
        # "Completed" is what the Boss or the user says when they judge
        # it so (complete_task), and this card said it for every turn
        # end. Without a result it is simply available.
        return f"Answered — {summary}" if summary else "Standing by"
    if status == "waiting_for_approval":
        return f"Needs your approval — {_one_line(approval)}" if approval \
            else "Needs your approval"
    if status == "waiting_for_input":
        return f"Needs your input — {_one_line(question)}" if question \
            else "Needs your input"
    if status == "paused":
        return "Paused"
    if status == "interrupted":
        return "Interrupted"
    if status == "recovering":
        return "Recovering…"
    if status == "completed":
        return f"Completed — {summary}" if summary else "Completed"
    if status == "failed":
        return f"Failed — {summary}" if summary else "Failed"
    if status == "cancelled":
        return "Cancelled"
    return status.replace("_", " ").capitalize()


def _glyph(state: dict) -> str:
    status = state.get("status") or "starting"
    if status in ATTENTION_STATES:
        return "attention"
    if status == "failed":
        return "failed"
    if status in FINISHED_STATES:
        return "done"
    if status == "idle" and (state.get("result") or {}).get("summary"):
        return "done"
    return "working"


def _one_line(text: str) -> str:
    """The first sentence, without its full stop: it follows a dash on a
    card, not a paragraph."""
    return " ".join(plain_text(text or "").split()).split(". ")[0] \
        .strip().rstrip(".")
