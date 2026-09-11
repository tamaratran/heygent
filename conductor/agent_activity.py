"""What a worker is doing, in a vocabulary the card can show.

A worker narrates itself in prose and tool calls - "I'm going to look
through the implementation and then I'll probably check the existing
tests..." / `Bash(pytest -q tests/)` - and the card was printing that
prose. The card should say what the worker is DOING, in a handful of
words that mean the same thing whichever provider produced them:

    Inspecting repository...
    Editing auth flow...
    Running tests...

So provider events carry a normalized activity, chosen here from the tool
and the text, and the card maps it to a label. Prose is kept only as the
detail after the dash, shortened.
"""

from __future__ import annotations

import re

ACTIVITIES = ("inspecting_repository", "reading_files", "editing",
              "running_command", "running_tests", "building", "checking_git",
              "waiting", "other")

LABELS = {
    "inspecting_repository": "Inspecting repository",
    "reading_files": "Reading files",
    "editing": "Editing",
    "running_command": "Running a command",
    "running_tests": "Running tests",
    "building": "Building",
    "checking_git": "Checking git",
    "waiting": "Waiting",
    "other": "Working",
}

_READ_TOOLS = ("Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch")
_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_TESTS = re.compile(r"\b(pytest|unittest|npm test|yarn test|jest|vitest|"
                    r"go test|cargo test|rspec|mocha|phpunit)\b")
_BUILD = re.compile(r"\b(npm run build|yarn build|cargo build|go build|"
                    r"make\b|tsc\b|webpack|vite build|gradle|mvn|xcodebuild)")
_GIT = re.compile(r"\bgit\b|\bgh\b")
_INSPECT = re.compile(r"\b(ls|find|tree|wc|du|cat|head|tail)\b")


def classify(summary: str, detail: dict | None = None) -> tuple[str, str]:
    """(activity, short detail) for one progress event.

    The detail is what follows the label on the card - the command or the
    file, cut short - never the worker's whole sentence.
    """
    detail = detail or {}
    tool = str(detail.get("tool") or "")
    text = " ".join((summary or "").split())
    gist = _gist(text)
    if tool in _EDIT_TOOLS or text.startswith(tuple(t + "(" for t in _EDIT_TOOLS)):
        return "editing", gist
    if tool in _READ_TOOLS or text.startswith(tuple(t + "(" for t in _READ_TOOLS)):
        return "reading_files", gist
    if tool == "Bash" or text.startswith("Bash("):
        command = gist.lower()
        if _TESTS.search(command):
            return "running_tests", gist
        if _BUILD.search(command):
            return "building", gist
        if _GIT.search(command):
            return "checking_git", gist
        if _INSPECT.match(command):
            return "inspecting_repository", gist
        return "running_command", gist
    if tool:
        return "running_command", gist
    if text.startswith("> "):
        # An instruction arriving in the worker's window, not the worker
        # doing something. Nothing to classify.
        return "waiting", ""
    return "other", _first_sentence(text)


def label(activity: str, detail: str = "") -> str:
    """The card's status text for an activity: the label, and the detail
    after a dash when there is one worth showing."""
    head = LABELS.get(activity, LABELS["other"])
    if detail:
        return f"{head} — {detail}"
    return head + "…"


def _gist(text: str) -> str:
    """`Bash(pytest -q tests/)` -> `pytest -q tests/`; a bare name stays.
    A path keeps its last two segments: `auth/flow.py`, not the volume."""
    match = re.match(r"^\w+\((.*)\)$", text)
    inner = match.group(1) if match else text
    inner = " ".join(inner.split())
    if "/" in inner and " " not in inner:
        inner = "/".join(inner.rstrip("/").split("/")[-2:])
    return inner[:57] + "…" if len(inner) > 60 else inner


def _first_sentence(text: str) -> str:
    """The first sentence of the worker's prose, shortened - the only part
    of a narration that fits on a card."""
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].strip()
    return first[:57] + "…" if len(first) > 60 else first
