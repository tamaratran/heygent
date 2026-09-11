"""The Boss's orchestration tools: one definition, three consumers.

The SDK backend registers these in-process; boss-mcp serves them to a
real Claude Code session over MCP; the startup gate checks that the ones
a Boss cannot do without are present. All three read from here, so the
Boss sees the same tools whichever way it runs, and none of them needs
the others' dependencies - this module imports nothing.

PROTOCOL_VERSION is the contract between boss-mcp and the Conductor's
bridge. Bump it when a tool's meaning or the wire format changes; a
helper reporting a different version is refused at the handshake rather
than run partially.
"""

from __future__ import annotations

PROTOCOL_VERSION = 1
SERVER_NAME = "boss"                 # tools appear as mcp__boss__<name>

# The longest title the Boss may give a task. A title is read at a glance -
# the notification panel, the cmux sidebar, the menu - and one that did not
# fit was cut there and given an ellipsis. Capping it where it is written
# means what the Boss chose is what the user reads, whole.
TITLE_MAX_CHARS = 40


def check_title(title: str) -> str:
    """The title as it will be stored: whitespace folded, and cut at a
    word to the limit if the Boss overshot it anyway. Cut here, not
    refused: an error would cost the user a second Boss turn for a
    title, and the Boss was told the limit up front."""
    title = " ".join(str(title or "").split())
    if not title:
        raise ValueError("create_task needs a title")
    if len(title) > TITLE_MAX_CHARS:
        title = fit_title(title)
    return title


def fit_title(title: str) -> str:
    """The longest prefix within the limit that ends at a word, without a
    dangling mark ("Fix login," is not a title). No ellipsis: the card is
    what the user reads, and "..." says "there was more" about a title
    that was already a summary."""
    cut = title[:TITLE_MAX_CHARS]
    if len(title) > TITLE_MAX_CHARS and title[TITLE_MAX_CHARS] != " " \
            and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-–—(") or title[:TITLE_MAX_CHARS]

# A Boss that cannot do these is not a functioning Boss. Startup refuses
# to mark it ready without them.
REQUIRED_TOOLS = ("list_open_sessions", "inspect_task",
                  "create_task", "send_to_task", "focus_task")

# The spec's vocabulary for the same operations, for anyone reading the
# timeline or the prompt: the tool names are the Conductor's.
SPEC_NAMES = {
    "create_task": "spawn_subagent", "send_to_task": "message_subagent",
    "inspect_task": "inspect_subagent", "focus_task": "focus_subagent",
    "pause_task": "pause_subagent", "resume_task": "resume_subagent",
    "interrupt_task": "interrupt_subagent", "cancel_task": "cancel_subagent",
    "approve_task_action": "approve_subagent_action",
    "deny_task_action": "deny_subagent_action",
}

SCHEMAS = {
    # project tools
    "list_projects": {},
    "inspect_project": {"project_id": str},
    # task tools
    "create_task": {"project": str, "title": str, "goal": str,
                    "background": bool, "computer": bool, "provider": str},
    "list_tasks": {},
    "list_subagents": {},
    "list_open_sessions": {},
    "list_recent_sessions": {},
    "search_sessions": {"query": str},
    "situation": {},
    "pause_task": {"task_id": str},
    "inspect_task": {"task_id": str},
    "send_to_task": {"task_id": str, "message": str},
    "interrupt_task": {"task_id": str},
    "resume_task": {"task_id": str},
    "complete_task": {"task_id": str},
    "cancel_task": {"task_id": str},
    "handoff_task_context": {"from_task_id": str, "to_task_id": str,
                             "note": str},
    "focus_task": {"task_id": str},
    "approve_task_action": {"task_id": str, "approval_id": str},
    "deny_task_action": {"task_id": str, "approval_id": str},
    # for the voice, never for the user's ears
    "note_for_voice": {"text": str},
    "what_the_voice_said": {},
    "tell_user": {"text": str},
}

# Parameters a caller may leave out, with what the tool sees then. Every
# schema key not named here is required. A Boss that predates a new
# parameter must still be able to call the tool: "an ordinary task" was
# a valid create_task before provider existed, and stays one.
OPTIONAL = {
    "create_task": {"provider": ""},
}

DESCRIPTIONS = {
    "list_projects": "List known projects with active-task counts.",
    "inspect_project": "One project's context and its tasks.",
    "create_task": "Start a new coding task in a project. provider is "
                   "optional: which CLI does the work - 'claude-code' "
                   "(default) or another the capability list says is "
                   "available, e.g. 'gemini'; pass it only when the user "
                   "names one. project is the "
                   "project as the user said it - a name, a path, or an "
                   "id - resolved and registered for you, so call this "
                   "directly; an ambiguous "
                   "name comes back as an error naming the candidates. "
                   "title is what "
                   "the user reads on its card and in the sidebar: at most "
                   f"{TITLE_MAX_CHARS} characters; a longer one is cut at "
                   "a word. "
                   "Its session opens visibly unless background is true."
                   " location is optional: 'local' (default, this "
                   "machine, sees uncommitted work, notifies you) or "
                   "'cloud' (Anthropic's infrastructure, readable at "
                   "claude.ai/code, gets a copy of the tree and stays "
                   "silent unless asked). Pass it only when the user "
                   "says where. computer is optional and off by default: "
                   "true only when the user asks the worker to operate "
                   "this machine's screen - click, type, read what is on "
                   "it. Such a worker's GUI actions still ask the user "
                   "for approval one by one.",
    "list_tasks": "List every task across all projects, open or not.",
    "list_open_sessions": "The sessions the user has open right now - a "
                          "live card on screen with unfinished work. This "
                          "is the answer to 'how many agents do I have "
                          "open?'. It is not every task on disk and not "
                          "every resumable provider session.",
    "list_recent_sessions": "Sessions that finished recently and are still "
                            "on screen. Visible, but not open.",
    "search_sessions": "Older work: finished or retired sessions, still "
                       "resumable. Use this for 'the login one from "
                       "yesterday'.",
    "situation": "One ready-to-speak paragraph covering everything in "
                 "flight, blocked work first. The answer to 'what's going "
                 "on?', 'where are we', 'give me a status' - say it as "
                 "written rather than recomposing it.",
    "list_subagents": "Every worker's live status and current activity "
                      "(working, idle, waiting for approval/input, paused, "
                      "interrupted, recovering, completed) - the answer to "
                      "'what is everything doing?'.",
    "pause_task": "Deliberately suspend a task for later; the session "
                  "stays alive and resume_task picks it back up.",
    "inspect_task": "Full detail of one task: state, context, events.",
    "send_to_task": "Send a follow-up, constraint or instruction to a task.",
    "interrupt_task": "Pause a task's current work (temporary).",
    "resume_task": "Let an interrupted task continue.",
    "complete_task": "Close a task whose work is done: its session ends, "
                     "its window closes, its branch and findings are "
                     "kept. Only the user decides a task is done - a "
                     "worker that has answered is waiting for more, not "
                     "finished. Not a cancel: cancel abandons the work.",
    "cancel_task": "Cancel a task permanently, abandoning the work.",
    "handoff_task_context": "Give one task another task's findings - sends "
                            "the source task's semantic context to the "
                            "destination task (works across projects).",
    "focus_task": "Bring a task's visible session window to the "
                  "foreground (reopening it if closed). Pure navigation - "
                  "the worker receives no message.",
    "approve_task_action": "Approve one pending action by its approval_id "
                           "(from inspect_task). Never approve blindly.",
    "deny_task_action": "Deny one pending action by its approval_id.",
    "note_for_voice": "The user will NOT hear this. Tell the voice something "
                      "the user should not hear "
                      "read out but may ask about next: task ids, file "
                      "paths, PR numbers, what not to promise. Call it "
                      "alongside your answer; the voice keeps the note "
                      "and answers follow-ups from it without waking you. "
                      "It is never spoken and never shown.",
    "tell_user": "Say something to the user now, in one or two sentences "
                 "- the spoken counterpart of note_for_voice. For a worker "
                 "update you have judged worth hearing: a result, a task "
                 "you just completed. Not for mid-work turn ends, repeats, "
                 "or approvals and questions (those are announced for "
                 "you). Not during your own turn: your reply is already "
                 "spoken.",
    "what_the_voice_said": "What the voice has said aloud to the user "
                           "lately, newest last, with how long ago. The "
                           "voice answers greetings and small talk and "
                           "fills gaps on its own, so the user may have "
                           "heard things you never said. Call it before "
                           "every reply, and phrase your answer as the "
                           "next sentence of that same conversation: do "
                           "not greet again, do not repeat, and never "
                           "contradict what was already said.",
}

# What the Boss is told about being a Boss, on top of the manager prompt.
# The tool names alone do not explain the product; this does.
ORIENTATION = """
## How you are running

You are the user's Boss: a persistent Claude Code session that supervises
coding workers rather than doing the coding yourself. The user talks to
you by voice and by typing into this window; both are turns of this one
conversation.

Your orchestration tools are the `boss` MCP server (`mcp__boss__*`):
finding projects, starting workers (create_task), inspecting them
(inspect_task, list_subagents, list_open_sessions), messaging them
(send_to_task), opening their windows (focus_task), and approving or
denying what they ask (approve_task_action, deny_task_action). Use them.
You have no file, shell or web tools on purpose: anything that needs
them - looking something up online, reading code, running a command - is
work you start, never a reason to tell the user you cannot. The user never
hears about workers or what you lack; see How you sound.

Every worker you start is a persistent child session of this one, with
its own window. Never invent a task, session or approval id: use the ids
that tool results give you. Everything you do here - each message, each
tool call and its result, each worker started or messaged, each worker
event you are told about - is recorded in this session's timeline for
the user to read back.

A worker is a coding CLI in its own window, and there is more than one
kind: Claude Code (the default), Codex, Cursor, Gemini, Devin, Droid,
and others as they are installed. The "Capabilities this run" list below says which are
available on this machine right now - trust it. When the user names
one ("start a Codex on this", "have Cursor do it"), pass it as
create_task's `provider` ("codex", "cursor", "gemini", "devin",
"droid"; a CLI listed
as `Name (provider "x")` is started with provider "x"); when they do
not, leave it out and Claude Code does the work. Every kind gets the
same window, card and messages; only the CLI inside differs.

What the user says arrives as their words, nothing added. The current
state is not attached to each turn: when you need it, ask for it -
`situation` for the whole picture, `list_open_sessions` for what is on
screen (with the ordinals the user refers to), `inspect_task` for one
worker. Your workers' turns are typed into this session as they happen
("Your worker · ..."), so you are told without asking.
"""
