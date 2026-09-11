"""What's going on?

Five asynchronous agents are only manageable if one sentence can hold them.
The Manager could compose that freely from list_subagents, but then the
answer's shape depends on the model's mood: sometimes a list, sometimes a
paragraph, sometimes an apology for how much is happening. Composing it here
makes it consistent, ordered by what needs the user, and testable.

The ordering is the useful part. Things that block on a decision come first,
because they are costing time right now; finished work comes last, because
it is only news. Within that, the wording is plain: what it is, what it did,
what it needs.
"""

from __future__ import annotations

# What each state means to someone deciding where to look next.
BLOCKED = ("waiting_for_approval", "waiting_for_input")
WORKING = ("starting", "working", "running", "recovering")
RESTING = ("waiting_for_user", "idle", "paused", "interrupted")
FINISHED = ("completed", "failed", "cancelled")

_ORDER = {s: i for i, group in enumerate((BLOCKED, WORKING, RESTING, FINISHED))
          for s in group}


def _clause(session: dict) -> str:
    """One session, in a sentence a person would say."""
    title = (session.get("title") or "a task").rstrip(".")
    state = session.get("state") or session.get("status") or ""
    activity = " ".join(str(session.get("activity") or "").split())
    detail = " ".join(str(session.get("result_summary") or "").split())

    if state in ("waiting_for_approval",):
        what = activity or "permission to continue"
        return f"{title} needs your approval: {what}"
    if state in ("waiting_for_input",):
        return f"{title} is waiting on you{': ' + activity if activity else ''}"
    if state == "failed":
        return f"{title} failed{': ' + detail if detail else ''}"
    if state == "completed":
        return f"{title} finished{': ' + detail if detail else ''}"
    if state == "cancelled":
        return f"{title} was cancelled"
    if state in ("paused",):
        return f"{title} is paused"
    if state in ("interrupted",):
        return f"{title} is stopped and can carry on"
    if state in ("waiting_for_user", "idle"):
        return f"{title} is idle, waiting for your next instruction"
    return f"{title} is {activity.lower()}" if activity else f"{title} is working"


# Spoken, not printed: "1 of them" reads as a list item, "one of them" as
# speech, and this text is going through a voice.
_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six",
          7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten"}


def _spell(n: int) -> str:
    return _WORDS.get(n, str(n))


def _count_phrase(n: int, blocked: int) -> str:
    if n == 0:
        return "Nothing is running."
    head = (f"{_spell(n)} thing is going." if n == 1
            else f"{_spell(n)} things are going.")
    if blocked == n and n:
        head = (f"{_spell(n)} thing is going, and it needs you." if n == 1
                else f"{_spell(n)} things are going, and they all need you.")
    elif blocked:
        head += (f" {_spell(blocked)} of them needs you." if blocked == 1
                 else f" {_spell(blocked)} of them need you.")
    return head


def situation_report(sessions: list[dict], recent: list[dict] | None = None,
                     limit: int = 5) -> str:
    """One spoken paragraph covering everything in flight.

    `sessions` are the open ones; `recent` are finished cards still on
    screen, mentioned briefly at the end rather than counted as active.
    """
    ordered = sorted(sessions,
                     key=lambda s: _ORDER.get(s.get("state") or
                                              s.get("status") or "", 9))
    blocked = sum(1 for s in ordered
                  if (s.get("state") or s.get("status")) in BLOCKED)
    parts = [_count_phrase(len(ordered), blocked)]
    shown = ordered[:limit]
    parts.extend(_clause(s) + "." for s in shown)
    if len(ordered) > limit:
        parts.append(f"And {len(ordered) - limit} more.")

    for session in (recent or [])[:2]:
        parts.append(_clause(session) + ".")
    return " ".join(parts)
