"""Configuration for the voice agent: models, voice, tools, and prompts.

Everything here is meant to be edited without touching voice_agent.py. The two
prompts live in prompts/*.md so they can be rewritten as prose; the rest are
plain constants.
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"

# --- models and voice -------------------------------------------------
# The generally available GPT Live model.
LIVE_MODEL = "gpt-live-1"
LIVE_VOICE = "marin"          # alloy ash ballad cedar coral echo marin sage shimmer verse

# --- what the delegated Claude session may do -------------------------
# allowed_tools only auto-approves; it does not take anything away. Under
# bypassPermissions the model will reach for Bash unless it is removed
# outright, so read-only mode has to name what it forbids.
READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]
WRITE_TOOLS = READ_ONLY_TOOLS + ["Edit", "Write", "Bash"]
BLOCKED_WITHOUT_WRITE = ["Bash", "Edit", "Write", "NotebookEdit",
                         "Task", "WebFetch", "WebSearch"]
MAX_TURNS = 6                 # how far one work item may go on its own

# How a worker session starts. "auto" lets it get on with ordinary work
# without stopping to ask, while still gating what the provider considers
# consequential - the middle ground bypassPermissions did not offer, which
# waved through everything including Bash.
#
# A worker is in its own git worktree on its own branch, so file changes
# are contained; Bash is not, and can still install, fetch and touch things
# outside that tree. That is exactly what auto still weighs, and it is why
# this is no longer bypass.
#
# There is no read-only worker: a delegated agent that stops for every edit
# is one nobody is there to answer. Anything auto does escalate reaches the
# user through the approval path rather than dying in an unwatched pane.
WORKER_PERMISSION_MODE = "auto"

# The equivalent for other providers, for when their runtime lands.
PROVIDER_BYPASS_FLAGS = {
    "claude-code": ["--permission-mode", "bypassPermissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox"],
}

# --- how answers reach the user -----------------------------------------
# "speak" - said aloud only, no card
# "text"  - card only, silent
# "both"  - said aloud and shown on the card (default)
REPLY_MODE = "both"

# The card's title already shows the transcribed request, so a live caption
# strip repeats it. Set True to show it anyway - useful when the microphone is
# mishearing and you want to see it happen in real time.
SHOW_CAPTION = False

# Your words inside the capsule while you hold the key, as the transcript
# lands: the newest under a soft highlight that pulses with your voice, the
# older ones dimming behind it, the capsule stretching to fit. They take the
# place of the bars while you talk; False brings the bars back.
CAPSULE_WORDS = True

# --- overlay sizing ------------------------------------------------------
# Points, not pixels, so these are already density independent - but screens
# differ, so tune them here rather than in overlay.py.
# The capsule's width is derived from the waveform so it always hugs it,
# rather than being a fixed box with the bars floating inside.
# These match Wispr Flow's recording pill, measured on screen: a 73x30 pt
# capsule, ten 2 pt bars on a 4 pt pitch, sitting 100 pt off the bottom.
PILL_BARS = 10                # bars in the scrolling waveform
PILL_BAR_WIDTH = 2.0
PILL_BAR_GAP = 2.0
PILL_PAD = 17.5               # space between the end bars and the rim
PILL_HEIGHT = 30.0
PILL_BOTTOM_MARGIN = 100.0    # gap from the bottom of the screen
CARD_WIDTH = 340.0

# --- how much the agents interrupt you ------------------------------------
# Voice becomes unusable if five workers narrate themselves, so speech is
# rationed rather than tied to whether something happened.
#
#   silent          nothing is spoken; the cards still update
#   attention       only what blocks you or broke (the default). A finish
#                   is not announced mechanically at all: it reaches the
#                   Boss as a worker update, and the Boss decides whether
#                   you hear about it, and in what words (tell_user).
#   important       the above, plus every worker turn end read out as a
#                   finish - before anyone judged whether it was one
#   conversational  the above, plus progress
#
# Cards are unaffected: the Activity Center always follows every session.
NOTIFY_MODE = "attention"

# --- where workers run ---------------------------------------------------
# "local"         a Claude Code session in a tmux pane, in its own git
#                 worktree here (the default; nothing extra to sign up for)
# "claude-cloud"  a Claude Code CLOUD session: runs on Anthropic's
#                 infrastructure, appears at claude.ai/code (so it is
#                 visible from a phone), and uses the Claude login the user
#                 already has - no API key, no third-party account.
#
# A cloud worker is BLIND until teleported: the CLI cannot read a cloud
# session's transcript back ("a cloud session cannot message other sessions
# back yet"), so there are no completions, no approvals and no status until
# `claude --teleport` brings it down to a local checkout - which is what
# focusing one does. A cloud session also refuses bypassPermissions, so it
# will stop and ask with nobody watching. Both are the provider's rules.
WORKER_LOCATION = "local"

# --- where a cloud session is shown --------------------------------------
# The browser, because only it can address a session. Claude for Mac
# registers three deep links - new, needs-input, continue?session=last -
# and none accepts a session id, so claude://code/<id> opens the app and
# lands wherever it already was, not on the work you asked for.
# Set this True if the app ever gains a route that takes an id.
CLOUD_PREFER_APP = False

# How often to go and look at a worker still in the cloud. Nothing is
# pushed to us - a cloud session cannot report back - so noticing that one
# has answered means teleporting into a throwaway checkout and reading it,
# which costs a process and several seconds. Minutes-scale on purpose: the
# difference between finding out on your own and having to ask, not a live
# feed. 0 turns it off, and a worker teleported down uses the fast path.
CLOUD_POLL_SECONDS = 90.0

# --- where a worker is visible -------------------------------------------
# "terminal"  a tmux pane, attachable in Terminal.app (today's default)
# "cmux"      a cmux workspace: the sidebar UI built for coding agents
#
# cmux is a hard dependency when selected, never a preference that silently
# degrades: if it cannot be found, driven, or is too old, startup says so
# and stops rather than quietly running workers somewhere the user is not
# looking. cmux is GPLv3 and this product bundles nothing, so installing it
# is first-run setup - `python3 -m conductor.cmux_setup` prints what to do
# and is what CI runs.
WORKER_SURFACE = "cmux"        # or "terminal"; --worker-surface overrides

# --- how long the voice waits on the Manager -----------------------------
# One spoken request is one Manager turn, and turns run one at a time in the
# order they were said: a request made while the Manager is busy waits its
# turn. This is how long the voice side waits for a turn (queueing included)
# before telling the user it is still in progress and moving on. The turn
# itself is never cut short - its answer is spoken when it does arrive - so
# this only bounds how long the voice side waits before saying so.
MANAGER_TURN_TIMEOUT_S = 300.0

# --- when an unaddressed task is closed ------------------------------------
# A worker that has answered waits at its prompt in case there is a
# follow-up, so its task is "waiting for you" until someone closes it - and
# one whose window died is "interrupted" until someone resumes it. Nothing
# used to close either, so every session ever started stayed on screen.
#
# A task nobody has addressed for this long is closed by the watchdog the
# same way you would close it: its session ends, its branch and context
# survive, its directory is given back only if it holds nothing uncommitted,
# and the session stays resumable from history. Speaking to a task resets
# its clock. 0 turns this off.
IDLE_RETIRE_S = 6 * 3600.0

# --- the watchdog --------------------------------------------------------
# How often to sweep for wedged workers: boot dialogs waiting on a keypress
# nobody will give, and tasks still marked running whose PTY has gone. Both
# are silent failures - the session simply stops answering - so they are
# found on a timer rather than when someone happens to look.
SWEEP_INTERVAL_S = 30.0

# --- the waiting line ----------------------------------------------------
# Claude Code shows a whimsical gerund while it works. One is picked per
# delegation and shown with the elapsed time, e.g. "✻ Cultivating… (12s)".
THINKING_WORDS = [
    "Cultivating", "Pondering", "Percolating", "Ruminating", "Simmering",
    "Noodling", "Marinating", "Cogitating", "Brewing", "Conjuring",
    "Finagling", "Herding", "Puzzling", "Spelunking", "Synthesizing",
    "Wrangling", "Deliberating", "Tinkering", "Unfurling", "Whirring",
]
THINKING_GLYPHS = ["✳", "✻", "✽", "✻"]      # cycled to animate

# --- prompts ----------------------------------------------------------
def _load(name: str, fallback: str) -> str:
    """Read a prompt file, taking everything after the `---` divider."""
    path = PROMPTS / name
    try:
        text = path.read_text()
    except OSError:
        return fallback
    _, _, body = text.partition("\n---\n")
    return (body or text).strip() or fallback


FRONTEND_INSTRUCTIONS = _load(
    "voice_agent.md",
    "You are the voice of Claude Code. The client runs everything the user "
    "says itself; speak briefly while it works and relay its answers "
    "conversationally.")

CLAUDE_SYSTEM_PROMPT = _load(
    "claude_worker.md",
    "Your answer is spoken aloud. At most three short sentences of plain prose.")
