"""The Boss as a session you can see.

Until now the Manager was an SDK call: a real Claude Code session, with a
session id and a transcript on disk, but running invisibly, and the only
trace of what it decided was whatever leaked into a notification card.
Codex's model is the one that works - one visible session you are
talking to, which is the session that creates the others.

So the Boss is hosted the way a worker is: a claude process in its own
cmux workspace, watched through its transcript, typed into through the
PTY. What changes for the Boss specifically:

  - its tools are the conductor's, served by boss-mcp - our own MCP
    server, on a runtime we control - over an authenticated socket to
    the conductor, instead of in-process functions;
  - its instructions live in CLAUDE.md in its own directory, which Claude
    Code loads natively and which the user can open;
  - its session id is the one the invisible Boss already had, resumed, so
    the conversation continues in the window it never had before;
  - everything it does is written to a BossSession timeline - what the
    user said, what it answered, every tool with its result, every worker
    it started or messaged, every worker event it received - so the
    record does not depend on any window having stayed open.

The window IS the execution. There is no hidden Boss behind it: a spoken
utterance is typed into this session, a typed line is a turn of the same
session, and both land in one ordered timeline.

And a Boss without its tools is not a Boss. Starting one is a sequence
of checks, each of which can refuse:

    helper present and executable, speaking this protocol
    credential minted, bridge bound to it
    session launched with a config naming that helper
    boss-mcp said hello, with the required tools
    ONLY THEN: ready
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .agent_events import AgentEvent
from .boss_session import (CHILD_ACTIONS, BossSession, BossSessionStore,
                           input_summary, new_boss_id, output_summary)
from .boss_tools import ORIENTATION, REQUIRED_TOOLS, SERVER_NAME
from .manager import ManagerBackend, ManagerTurn, ToolCall, users_words
from .observability import ObservabilityEvent, application_log
from .watch_link import without_watch_links

BOSS_TASK_ID = "boss"
# Tools the Boss must not have: it conducts, it does not code. The same
# list the SDK backend disallowed.
DISALLOWED = ("Bash", "Read", "Edit", "Write", "Glob", "Grep",
              "NotebookEdit", "Task", "Agent", "WebFetch", "WebSearch",
              "AskUserQuestion")
# "Agent" is what Claude Code 2.1.250 calls the subagent tool ("Task" in
# older builds); a Boss with it could spawn workers the Conductor never
# hears of. Both names are refused. "MultiEdit" is gone from current
# builds (folded into "Edit", which stays denied); naming it makes
# Claude Code warn that the deny rule matches no known tool.
# "AskUserQuestion" renders an option menu in the pane; the Boss's answers
# arrive as typed text, which moves the highlight but never submits, and
# the turn hangs unread. Denied, it asks in prose like everything else.
# How the Boss starts (asked 2026-09-10): Opus, low reasoning, fast mode, focus
# view. It routes work rather than doing it, so speed beats depth. Model
# and effort are flags; the rest are session-only settings (--settings), never
# written to the user's files. fastMode from --settings holds even where
# fastModePerSessionOptIn would start a session with it off. Focus view
# needs the fullscreen renderer ("tui": "fullscreen"), so that comes too.
BOSS_MODEL = "opus"          # the alias: always the current Opus
BOSS_EFFORT = "low"
# No thinking at all (asked the same day for "zero effort"): low is the
# lowest --effort there is, so alwaysThinkingEnabled false does the rest.
BOSS_SETTINGS = {"fastMode": True, "alwaysThinkingEnabled": False,
                 "tui": "fullscreen", "viewMode": "focus"}
# How long the launched session gets to bring boss-mcp up and say hello.
MCP_CONNECT_TIMEOUT_S = 90.0
# How the Boss reaches its tools: "stdio" starts boss-mcp, the packaged
# helper, as a child of the session; "http" points the session at the
# conductor's own loopback endpoint (conductor_mcp) and starts nothing.
TRANSPORTS = ("stdio", "http")


class BossUnavailable(RuntimeError):
    """Boss orchestration tooling is unavailable: a Boss was not started,
    or was started and could not reach its tools. Never partially."""



def whole_sentences(text: str, limit: int) -> str:
    """text, cut at the last sentence end that fits in limit - or at a word
    if no sentence does - with an ellipsis when anything was cut."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut > limit // 3:
        return head[:cut + 1]
    return head.rsplit(" ", 1)[0] + "…"

@dataclass
class _VoiceTurn:
    """One utterance typed into the window, waiting for the Boss's answer.

    Several can be open at once: the words go in the moment they are
    said, and what arrives while the Boss is mid-turn sits in Claude
    Code's own input box as a queued message until it looks up.
    """
    text: str
    started: float
    # The session has read it: its transcript shows the message. Until
    # then it is queued in the box, and a turn end is not its answer.
    taken: bool = False
    # A later utterance has been read since this one was answered: the
    # answer it has is final, whatever the Boss says next.
    closed: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    reply: str = ""
    # Read together with an earlier utterance and answered once, on that
    # one. There is nothing to say for this one.
    folded: bool = False
    # Typed a second time after the session let the first go. One retype
    # per utterance: a second loss is final.
    retyped: bool = False


class PtyManagerBackend(ManagerBackend):
    _push_failures = 0          # class default: a backend built without __init__ (tests)

    def __init__(self, runtime, home: str | Path, bridge_socket: str | Path,
                 python: str, repo_root: str | Path,
                 model: str | None = BOSS_MODEL,
                 turn_timeout: float = 300.0,
                 store: BossSessionStore | None = None,
                 helper: str | Path | None = None,
                 connect_timeout: float = MCP_CONNECT_TIMEOUT_S,
                 transport: str = "stdio",
                 session_settings: dict | None = None) -> None:
        if transport not in TRANSPORTS:
            raise ValueError(f"transport must be one of {TRANSPORTS}")
        self.transport = transport
        # Claude Code settings for THIS session only (--settings <json>):
        # never written to the user's settings files. Measured use: a
        # Boss that should receive workers' cross-session replies needs
        # {"crossSessionInbound": "accept"}, because a session that
        # bypasses permission prompts holds peer messages otherwise.
        self.session_settings = {**BOSS_SETTINGS, **(session_settings or {})}
        self.effort: str | None = BOSS_EFFORT
        # Diagnostics only: Claude Code's own debug log for the Boss
        # process (--debug-file), when someone wants to see the MCP
        # client's side of a connection. Off by default.
        self.debug_file: str | None = None
        self.runtime = runtime
        self.home = Path(home)
        self.boss_dir = self.home / "boss"
        self.bridge_socket = Path(bridge_socket)
        self.python = python                      # kept for the headless path
        self.repo_root = Path(repo_root)
        self.model = model
        self.turn_timeout = turn_timeout
        self.connect_timeout = connect_timeout
        self.helper = Path(helper) if helper else None
        self.store = store or BossSessionStore(self.home)
        self.session: BossSession | None = None      # the durable record
        self.session_id: str | None = None           # the provider's id
        self._conductor = None
        self._bridge = None
        # One keyboard: sends do not interleave, and the session is
        # opened once. Not a turn lock - see handle.
        self._send_lock: asyncio.Lock | None = None
        self._turns: list[_VoiceTurn] = []          # open, oldest first
        # What to do with the Boss's first sentence of a slow turn: the
        # voice side speaks it. None means nobody asked.
        self.on_interim: Callable[[str], None] | None = None
        # Each paragraph of the Boss's reply as it is written: the window
        # draws it then, not when the turn ends. None means no window.
        self.on_prose: Callable[[str], None] | None = None
        self._interim_said = False
        self._interim_task: asyncio.Task | None = None
        self._interim_for: list[_VoiceTurn] = []     # the turns it would speak for
        self._turn_trace = ""
        self._unsubscribe = None
        self._executions: dict[str, str] = {}        # execution id -> tool
        self._credential: str = ""
        self._last_event_at = 0.0
        # Worker turns, pushed. Every meaningful turn of a worker this
        # Boss created is typed into its session as it happens, so the
        # Boss knows without being asked. Held while a voice turn is in
        # flight - a queued update would otherwise run straight after the
        # answer and be taken for part of it - and flushed as one line
        # when the turn ends.
        self.push_updates = True
        self._pending_updates: list[str] = []
        self._pushed_ids: list[str] = []
        self._push_failures = 0
        self._pushes_open = 0              # push turns the Boss has not ended
        self._push_texts: list[str] = []   # what pushes typed, for _take_turn
        self._told_user_in_push = False    # tell_user already spoke for it
        # The window is opened without focus and brought forward once,
        # on the voice turn SHOW_AFTER_TURNS names. Measured
        # before: cmux jumped in front at launch for a "hey", and the
        # user could not even quit it, because the Boss lives in it.
        self._voice_turns = 0
        self._window_shown = False
        # How the Boss's own window is raised. With the voice-agent window
        # open the Boss has no cmux workspace to select, so the launcher
        # sets this to raise that window instead; without it, the
        # runtime's bring_forward (cmux) is the answer.
        self.show_window: Callable[[], None] | None = None

    # The voice turn on which the Boss's window is first put in front of
    # the user: the first, so speaking to the Boss is what opens it.
    SHOW_AFTER_TURNS = 1

    # After a turn end, how long the session has to stay quiet before the
    # answer is taken as final. Measured: the Boss said "Retrying the
    # lookup..." (a turn end), then called two more tools and gave the real
    # answer; the voice had already spoken the first sentence.
    # After the Boss's turn ends, how long it must stay quiet before the
    # answer is taken. 2.5 s sat on every reply; 1.5 s still outlasts the
    # gap between a turn end and a follow-on tool call.
    SETTLE_S = 1.5
    # A sentence the Boss says before calling tools is spoken this long
    # after it appears, if the turn is still running then. Measured: "I'll
    # start two workers" came 4.6 s into a turn that took 30 s more, and
    # the user heard nothing until the end.
    INTERIM_AFTER_S = 1.5

    # -- ManagerBackend --------------------------------------------------------
    @property
    def busy(self) -> bool:
        """An utterance is with the Boss and its answer is not back. Not
        a reason to hold anything: the next thing said goes straight in
        (handle), and so does a worker update (deliver_supervisory) -
        which line a turn end answers is read off the transcript."""
        return bool(self._turns) or (self._send_lock is not None
                                     and self._send_lock.locked())

    @property
    def _in_voice_turn(self) -> bool:
        return bool(self._turns)

    def attach_bridge(self, bridge) -> None:
        """The tool host this Boss's calls arrive through: a BossBridge
        (stdio transport) or a ConductorMcp (http). Same hooks either way."""
        self._bridge = bridge
        bridge.on_start = self._tool_started
        bridge.on_finish = self._tool_finished

    async def handle(self, text: str, conductor) -> ManagerTurn:
        """Type the words in now; wait for the answer after.

        What the user says goes into the window the moment they say it,
        whatever the Boss is doing. Mid-turn, Claude Code keeps it in its
        own input box as a queued message - the user sees it land - and
        reads it when it next looks up: folded into the running turn, or
        as the turn after it. This used to be one lock around the whole
        turn, so the second thing said was not even typed until the
        first was ANSWERED. Measured 2026-08-29: a turn started at
        08:07:37, the next utterance came at 08:08:26 and was still not
        in the window at 08:11 - three minutes of the user's words held
        in this process while the Boss ran tools.

        Only the keyboard is shared (_send_lock); the wait is per
        utterance (_VoiceTurn), and which turn end answers which words
        is read off the transcript (_take_turn, _resolve_turns).
        """
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            session_id = await self._ensure_session(conductor)
            turn = await self._type_in(session_id, text, conductor)
        return await self._await_answer(session_id, turn, conductor)

    async def warm(self, conductor) -> bool:
        """Open the Boss now, rather than on the user's first words.

        Opening takes ~30 s: a cmux workspace, then Claude Code resuming
        a transcript that is 740 KB and growing. Measured on a live run,
        the first thing said after a restart waited all of it - the turn
        started at 00:14:14 and the Boss saw the words at 00:14:46. The
        cost is real; the moment was wrong. Under the turn lock, so an
        utterance that arrives meanwhile waits for this session instead
        of launching a second one. Failure is logged, not raised: the
        first turn will try again and report it properly.
        """
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        started = time.monotonic()
        async with self._send_lock:
            try:
                await self._ensure_session(conductor)
            except Exception:
                application_log("manager", "boss.warm_failed",
                                "the Boss could not be opened ahead of the "
                                "first turn; it will be tried again then",
                                severity="warning", exc_info=True)
                return False
        conductor.bus.emit(ObservabilityEvent(
            type="boss.warmed", component="manager",
            manager_session_id=self.session_id,
            duration_ms=round((time.monotonic() - started) * 1000, 1)))
        return True

    def rebind(self) -> None:
        """Forget the live session: the next turn opens the Boss bound
        to the store's current conversation. launch_session replaces
        the pane, so nothing is killed here; only the binding this
        manager holds is dropped."""
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        self.session = None
        self.session_id = None

    async def close(self) -> None:
        # The session stays open on purpose: it is the user's window onto
        # the Boss and outlives any one run of the app. Only the watcher
        # this process attached is released.
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    # -- the durable record --------------------------------------------------------
    def _ensure_record(self, conductor) -> BossSession:
        """One BossSession per voice conversation, bound on first use and
        kept: subsequent turns, spoken or typed, are the same session."""
        if self.session is not None:
            return self.session
        conversation = self.store.current_conversation()
        session = self.store.boss_for(conversation)
        if session is None:
            session = BossSession(id=new_boss_id(), conversation_id=conversation,
                                  workspace=str(self.boss_dir))
            # The invisible Boss's session id is inherited ONCE - by the
            # first visible Boss ever, so the conversation the user already
            # had carries into the window. Every record after that is a
            # conversation the user asked for anew, and it starts empty.
            # It did not: every new record inherited the id, so
            # `--new-chat` printed a fresh Boss id and then resumed the
            # old 800 KB conversation into it. The Boss woke up with all
            # its old context and, asked "what's up", re-created a task
            # the user had just cancelled.
            if not self.store.list():
                stored = (conductor.projects.manager().get("session_id")
                          if hasattr(conductor, "projects") else None)
                session.provider_session_id = stored
            self.store.save(session)
            self.store.bind(conversation, session.id)
            self.record("system_event", {"text": "Boss session created"})
            self._voice_turns = 0            # a new conversation starts quiet
            self._window_shown = False
        self.session = session
        if hasattr(conductor, "boss_store"):
            conductor.boss_store = self.store
        return session

    def record(self, type_: str, payload: dict | None = None,
               trace_id: str = "") -> None:
        if self.session is None:
            return
        try:
            self.store.append(self.session.id, type_, payload,
                              trace_id=trace_id or self._turn_trace)
        except OSError:
            application_log("manager", "boss.timeline_write_failed",
                            f"could not record {type_}", severity="warning",
                            exc_info=True)

    def _set_status(self, status: str) -> None:
        if self.session is None or self.session.status == status:
            return
        self.session.status = status
        self.store.save(self.session)

    # -- the session process -----------------------------------------------------------
    def _write_instructions(self, conductor=None) -> None:
        """The Boss's standing instructions: the manager prompt, how it is
        running, and the capability snapshot - which used to ride along
        in every turn and is static for a run, so it belongs here."""
        from .claude_manager import load_manager_prompt
        self.boss_dir.mkdir(parents=True, exist_ok=True)
        parts = ["# You are the Boss\n\n" + load_manager_prompt(), ORIENTATION]
        if conductor is not None:
            try:
                from .capabilities import capability_block
                parts.append("## Capabilities this run\n\n" + capability_block(conductor))
            except Exception:
                application_log("manager", "boss.capabilities_unavailable",
                                "could not snapshot capabilities for the Boss",
                                severity="warning", exc_info=True)
        (self.boss_dir / "CLAUDE.md").write_text("\n\n".join(parts) + "\n")

    def _resolve_helper(self) -> Path:
        """The packaged boss-mcp, verified. Refuses rather than guesses."""
        from .boss_helper import ensure_helper, verify_helper
        path = self.helper or ensure_helper(self.home, self.repo_root)
        check = verify_helper(path)
        if not check.ok:
            raise BossUnavailable(
                f"Boss orchestration tooling is unavailable: {check.problem}")
        return Path(path)

    def _write_mcp_config(self, helper: Path | None, names: tuple[str, ...],
                          boss_id: str, credential: str) -> Path:
        """Session-scoped: this file is named on THIS session's command
        line and nowhere else. The user's other Claude Code sessions never
        see the boss tools, and never see this file."""
        path = self.boss_dir / "mcp.json"
        if self.transport == "http":
            # The conductor's own endpoint; the credential rides in the
            # Authorization header of every request.
            entry = self._bridge.mcp_config_entry(credential)
        else:
            entry = {
                "type": "stdio",
                "command": str(helper),
                "args": ["--socket", str(self.bridge_socket),
                         "--tools", ",".join(names)],
                "env": {"BOSS_MCP_TOKEN": credential, "BOSS_SESSION_ID": boss_id},
            }
        path.write_text(json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2))
        path.chmod(0o600)
        return path

    def argv(self, names: tuple[str, ...], claude: str,
             session_id: str, mcp_config: Path, resume: bool) -> list[str]:
        """The Boss's command line. The session id is always known up
        front - resumed, or pinned for a fresh session - so the runtime
        never has to discover which transcript is the Boss's."""
        argv = [claude, "--permission-mode", "bypassPermissions",
                "--mcp-config", str(mcp_config), "--strict-mcp-config",
                "--allowedTools", ",".join(f"mcp__{SERVER_NAME}__{n}" for n in names),
                "--disallowedTools", ",".join(DISALLOWED)]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        if self.session_settings:
            argv += ["--settings", json.dumps(self.session_settings)]
        if self.debug_file:
            argv += ["--debug-file", self.debug_file]
        argv += ["--resume" if resume else "--session-id", session_id]
        return argv

    async def _ensure_session(self, conductor) -> str:
        record = self._ensure_record(conductor)
        if self.session_id and await self._alive(self.session_id):
            self._ensure_watched()
            return self.session_id
        self._conductor = conductor
        from .claude_manager import _manager_tools
        names = _manager_tools(conductor)
        self._write_instructions(conductor)
        # 1. the tool host: the packaged helper, verified (stdio), or the
        #    conductor's own endpoint, listening (http)
        if self._bridge is None:
            raise BossUnavailable("Boss orchestration tooling is unavailable: "
                                  "no bridge to the Conductor")
        if self.transport == "http":
            helper = None
            if not getattr(self._bridge, "port", None):
                raise BossUnavailable("Boss orchestration tooling is unavailable: "
                                      "the Conductor's MCP endpoint is not listening")
        else:
            helper = self._resolve_helper()
        # 2. the credential, and the host bound to it
        self._credential = self._bridge.new_token()
        self._bridge.expect(record.id, self._credential)
        mcp_config = self._write_mcp_config(helper, names, record.id,
                                            self._credential)
        # 3. the session, launched with a config naming that helper
        # The record says which provider session this Boss is. No fallback
        # to the global manager id here: _ensure_record already gave it to
        # the one record entitled to it, and reaching for it again is how
        # a new conversation resumed the old one.
        stored = record.provider_session_id
        existing = None
        if stored:
            # Resuming: the session's file already exists, and discovery
            # must land on it, not skip it as "already there". But Claude
            # Code scopes resume to the working directory: a session whose
            # transcript lives under another directory - the invisible
            # Boss's, which ran in the repo - cannot be resumed from here.
            # Say so and start fresh rather than launch a session that
            # never reaches its prompt.
            from .tmux_runtime import CLAUDE_PROJECTS, munge_project_dir
            project_dir = CLAUDE_PROJECTS / munge_project_dir(str(self.boss_dir))
            if not (project_dir / f"{stored}.jsonl").exists():
                self.record("system_event", {
                    "text": f"previous Boss session {stored[:8]} is not "
                            f"resumable from {self.boss_dir}; starting a new "
                            f"session in this window"})
                stored = None
            else:
                existing = {str(p) for p in project_dir.glob("*.jsonl")
                            if p.stem != stored}
        # Known up front, either way: the runtime adopts the session at
        # its prompt rather than waiting for a transcript that a session
        # nobody has spoken to yet will never write.
        boss_sid = stored or str(uuid.uuid4())
        argv = self.argv(names, self.runtime.claude, boss_sid, mcp_config,
                         resume=stored is not None)
        self._set_status("recovering" if stored else "starting")
        self.session_id = await self.runtime.launch_session(
            BOSS_TASK_ID, str(self.boss_dir), argv, existing=existing,
            session_id=boss_sid, focus=False)
        # The Boss's workspace announces itself in the sidebar: named,
        # pinned to the top, its own colour - the origin every routing
        # starts from, distinct at a glance from the workers it routes to.
        from .tmux_runtime import session_name
        if hasattr(self.runtime, "dress"):
            await asyncio.to_thread(self.runtime.dress,
                                    session_name(BOSS_TASK_ID),
                                    title="Boss", state="boss", pin=True)
        # Captured the moment it exists, not at the end of a turn: this
        # is what a restart resumes.
        if record.provider_session_id != self.session_id:
            record.provider_session_id = self.session_id
            self.store.save(record)
        if hasattr(conductor, "projects") and \
                conductor.projects.manager().get("session_id") != self.session_id:
            conductor.projects.set_manager("anthropic", self.session_id)
        self._unsubscribe = await self.runtime.subscribe(self.session_id,
                                                          self._on_event)
        # 4. boss-mcp said hello, with the required tools
        connected = await self._bridge.wait_connected(self.connect_timeout)
        if connected is None:
            self._set_status("failed")
            what = ("the session never connected to the Conductor's MCP endpoint"
                    if self.transport == "http" else "boss-mcp never connected")
            self.record("system_event", {"text": f"{what}; the Boss has no tools"})
            raise BossUnavailable(f"Boss orchestration tooling is unavailable: {what}")
        missing = [t for t in REQUIRED_TOOLS if t not in connected["tools"]]
        if missing:
            self._set_status("failed")
            self.record("system_event", {"text": f"boss-mcp is missing required "
                                                 f"tools: {missing}"})
            raise BossUnavailable("Boss orchestration tooling is unavailable: "
                                  f"required tools missing: {missing}")
        # 5. ready
        self._set_status("ready")
        self.record("system_event", {
            "text": ("Boss session resumed" if stored else "Boss session opened")
                    + f" ({self.session_id[:8]}); {len(connected['tools'])} tools"})
        conductor.bus.emit(ObservabilityEvent(
            type="boss.session_opened", component="manager",
            manager_session_id=self.session_id,
            data={"boss_session_id": record.id, "resumed": bool(stored),
                  "workspace": str(self.boss_dir),
                  "tools": len(connected["tools"])}))
        self.resync_updates()
        return self.session_id

    async def _alive(self, session_id: str) -> bool:
        try:
            status = await self.runtime.get_status(session_id)
        except Exception:
            return False
        # The runtimes answer with a plain string ("idle", "working",
        # "disconnected"...). Reading `.status` off a string gave None,
        # which read as dead: every turn killed and resumed the Boss - a
        # second connection on every run, a recycled window each turn.
        # Measured across two typed turns, tmux and cmux alike.
        state = status if isinstance(status, str) else \
            getattr(status, "status", None)
        if state in (None, "disconnected", "failed"):
            return False
        # The runtime answers for the WORKSPACE. cmux keeps a workspace -
        # and the last title in it - after the process inside has exited.
        # Measured: the Boss's claude was gone at 01:33, its workspace
        # still said "✳ Coding session", and for the next ten minutes
        # every message typed into an empty shell and failed after 45s
        # with "never returned to its prompt". The process is the Boss;
        # ask for it - when the runtime hosts PTYs at all, which is what
        # makes a window outlive its process. A runtime without panes
        # (the SDK one, the test fakes) has nothing to be wrong about.
        from .tmux_runtime import TmuxClaudeRuntime
        hosts_panes = isinstance(self.runtime, TmuxClaudeRuntime)
        if hosts_panes and not await asyncio.to_thread(self._process_running):
            self.record("system_event", {
                "text": "the Boss process is gone from its window; it "
                        "will be reopened on the next turn"})
            return False
        return True

    def _ensure_watched(self) -> bool:
        """A live Boss that nobody is reading answers into the void.

        Its replies reach the voice through the runtime's watcher on its
        transcript, and that watcher is a task that can end - it did, on
        one failed cmux listing, and the Boss was "alive" by every other
        measure for the next six hours while every turn timed out. The
        runtime knows whether it is still reading; ask it, every turn, and
        have it read again if not. Runtimes without watchers (the SDK
        one, the test fakes) have nothing to restart.
        """
        rewatch = getattr(self.runtime, "rewatch", None)
        if rewatch is None or not self.session_id:
            return False
        if not rewatch(self.session_id):
            return False
        self.record("system_event", {
            "text": "the Boss's transcript was not being read; reading "
                    "it again"})
        application_log("manager", "boss.watcher_restarted",
                        "the Boss's watcher had ended; restarted it",
                        severity="warning",
                        manager_session_id=self.session_id)
        return True

    def _process_running(self) -> bool:
        """Is there a claude running THIS Boss? Its argv names our MCP
        config, which nothing else on the machine does."""
        needle = str(self.boss_dir / "mcp.json")
        try:
            found = subprocess.run(["pgrep", "-f", needle],
                                   capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return True          # cannot tell: never retire a live Boss on a guess
        return bool(found.stdout.strip())

    # -- what the session does, as it does it -----------------------------------------
    def _on_event(self, event: AgentEvent) -> None:
        """The Boss's own transcript, watched like a worker's. A turn the
        user typed into the window arrives here too - as the same kind of
        event, into the same timeline."""
        self._last_event_at = time.monotonic()
        if event.type == "progress":
            source = (event.detail or {}).get("source", "")
            if source == "user_message":
                if not self._take_turn(event.summary):
                    text = event.summary[2:] if event.summary.startswith("> ") \
                        else event.summary
                    self.record("user_message", {"text": text, "source": "typed"})
                    self._set_status("thinking")
            elif (event.detail or {}).get("tool"):
                # A tool call is its own item (tool_started, from the
                # bridge). Assistant prose is not recorded here: it is the
                # reply, recorded once when the turn ends - recording it as
                # it streamed put every answer on the timeline twice.
                self._set_status("waiting_for_tool")
            elif source == "" and event.summary.strip():
                self._prose(event.summary, event.text or event.summary)
        elif event.type == "completed":
            self._on_turn_end(event)

        elif event.type == "failed":
            self._resolve_turns(f"That did not work: {event.error}")
            self.record("system_event", {"text": f"Boss failed: {event.error[:200]}"})
            self._set_status("failed")

    def _prose(self, summary: str, text: str) -> None:
        """A paragraph the Boss has just written, mid-turn: whole to the
        window, its 300-character summary to the interim.

        It goes to the window as it lands (on_prose), so the reply is
        read as it is written rather than all at once when the turn has
        ended and settled - asked 2026-09-10: "it's only after it
        finishes". Claude Code writes its transcript a content block at
        a time, so a paragraph is the finest grain there is.

        "This turn" is the utterances the session has READ and not
        answered. Not merely open: a queued utterance the session has
        not looked at yet cannot be what this sentence answers. Measured
        2026-08-30 23:32:11-13Z: "Hey - what do you want done?" was
        answered and handed to the voice at 11.742, its prose line
        reached the watcher a tick later, and with a second utterance
        queued the interim spoke the same words at 13.242 - "Hey, what
        do you want done? Hey, what do you want done?". A sentence with
        no read, unanswered utterance behind it is a straggler from an
        answered turn (or the Boss talking to a pushed update); nothing
        to show and nothing to say.
        """
        answering = [t for t in self._turns
                     if t.taken and not t.done.is_set()]
        if not answering:
            return
        if self.on_prose is not None:
            try:
                self.on_prose(text)
            except Exception:
                application_log("manager", "boss.prose_failed",
                                "the window missed a paragraph of the "
                                "Boss's reply", severity="warning",
                                exc_info=True)
        if not self._interim_said and self.on_interim is not None:
            # The Boss's first sentence of this turn. If the turn is
            # still running a moment from now, it went off to run
            # tools, and this sentence is what the user should hear
            # meanwhile rather than silence.
            self._interim_said = True
            self._interim_for = answering
            self._interim_task = asyncio.get_event_loop().create_task(
                self._say_interim(summary, answering))

    def _on_turn_end(self, event: AgentEvent) -> None:
        if not event.summary.strip():
            # A turn end with nothing said - Claude Code emits one on
            # startup and after some tool-only turns. Not an answer;
            # ending the voice turn on it returned "" to the user.
            # It IS the end of a pushed update's turn, though: left
            # marked as pushing, every later update queued behind it
            # until the next spoken turn - "never arrived", measured.
            if self._pushes_open:
                self._finish_push()
            return
        answered = self._resolve_turns(without_watch_links(event.summary))
        self._answer_claims_interim()
        source = "voice" if answered else \
            "worker_update" if self._pushes_open else "typed"
        self.record("boss_message", {"text": event.summary[:2000],
                                     "source": source})
        self._set_status("ready")
        if self._pushes_open:
            # What the Boss says back to a worker update is what the
            # user should hear about that worker - measured: it wrote
            # "Here's what PR fifty-nine does: ..." to an update and
            # the user heard nothing, because a reply to a push was a
            # timeline entry and it had used note_for_voice, the
            # channel nobody hears. Unless tell_user already spoke.
            if not self._told_user_in_push and self._conductor is not None:
                self._conductor.bus.emit(ObservabilityEvent(
                    type="boss.tell_user", component="manager",
                    manager_session_id=self.session_id,
                    data={"text": without_watch_links(event.summary)[:600],
                          "source": "worker_update"}))
            self._finish_push()

    # How much of a line is compared with what was typed: the watcher
    # keeps 200 characters of a user line, and Claude Code can join
    # several queued messages into one.
    MATCH_CHARS = 120

    @staticmethod
    def _norm(text: str) -> str:
        text = text[2:] if text.startswith("> ") else text
        return " ".join(text.split()).casefold()

    def _matches(self, line: str, text: str) -> bool:
        """Whether a user line (normalised) is, or contains, this text."""
        key = self._norm(text)[:self.MATCH_CHARS]
        if not key:
            return False
        if len(key) >= 12:
            return key in line
        # A short one ("do", "yes") is matched as a whole word.
        return line == key or line.startswith(key + " ") \
            or line.endswith(" " + key) or f" {key} " in line

    def _take_turn(self, line: str) -> bool:
        """The transcript shows a user message. Whose?

        Ours when it is what we typed - matched by its words, not by
        being next in line. It used to be next in line: the oldest
        utterance not yet read was taken to be this one. Measured
        2026-08-30 08:47:15: an utterance was typed and confirmed sent
        and Claude Code never wrote it (queued, then let go when the
        running turn ended); from then on every reply was credited to
        the utterance BEFORE the one it answered, the newest turn hung
        open until the next reply or the 300 s timeout, and worker
        updates - held while a turn was open - waited three minutes.

        A worker update's own line is ours too, and is not an
        utterance. A line matching nothing was typed by the user in the
        window. Returns whether the line was ours.
        """
        line = self._norm(line)
        for text in self._push_texts:
            if self._matches(line, text):
                self._push_texts.remove(text)
                return True
        read = [t for t in self._turns if not t.taken
                and self._matches(line, t.text)]
        if not read:
            return False
        newest = read[-1]
        for turn in self._turns:
            if turn is newest:
                break
            if not turn.taken and turn not in read:
                # Queued before these words, and never written: the
                # session let it go. Typed again, once; a second loss
                # is final.
                if turn.retyped:
                    self._lose(turn)
                else:
                    self._retype(turn)
        for turn in read:
            turn.taken = True
        for turn in self._turns:
            if turn not in read and turn.done.is_set():
                # Answered before these words were read: that answer is
                # final. What the Boss says next is for the new words,
                # not a correction to the old ones.
                turn.closed = True
        return True

    def _lose(self, turn: _VoiceTurn) -> None:
        turn.reply, turn.folded = "", True
        turn.done.set()
        self.record("system_event", {"text": "the Boss never read: "
                                             f"{turn.text[:200]}"})
        application_log("manager", "boss.words_lost",
                        "an utterance was typed and never read; a later "
                        "one was", severity="warning", text=turn.text[:300])

    def _retype(self, turn: _VoiceTurn) -> None:
        """An utterance the session queued and let go is typed again.

        Measured 2026-08-31 09:13:35 (run 1488): a barge-in - "Stop.
        Just tell me the number of tasks." - sat in Claude Code's input
        box while the Boss ran a turn, was dropped when that turn ended,
        and the user's interruption was never answered. The words are
        still ours to send; losing them should take two failures, not
        one.
        """
        turn.retyped = True
        self.record("system_event", {"text": "the Boss let this go "
                                             f"unread; typing it again: {turn.text[:200]}"})
        application_log("manager", "boss.words_retyped",
                        "an utterance was typed and never read; typing "
                        "it again", severity="warning", text=turn.text[:300])
        asyncio.get_event_loop().create_task(self._retype_send(turn))

    async def _retype_send(self, turn: _VoiceTurn) -> None:
        try:
            async with self._send_lock:
                if turn.done.is_set() or not self.session_id:
                    return
                await self.runtime.send(
                    self.session_id, self.compose(turn.text, self._conductor))
        except Exception:
            application_log("manager", "boss.retype_failed",
                            "the retyped utterance could not be sent",
                            severity="error", exc_info=True)
            if not turn.done.is_set():
                self._lose(turn)

    def _resolve_turns(self, reply: str) -> list[_VoiceTurn]:
        """A turn end. It answers every utterance the session has read
        and not yet closed - one, usually; several when the Boss read a
        queued message into the turn it was running. Read together means
        answered together: the oldest carries the reply and the rest are
        folded into it, so one answer is spoken once. A turn end with
        nothing read (the watcher missed the user line) still answers
        the oldest: a message is out, and this is the reply to something."""
        open_turns = [t for t in self._turns if not t.closed]
        answered = [t for t in open_turns if t.taken] or open_turns[:1]
        for index, turn in enumerate(answered):
            if index == 0:
                turn.reply = reply
            else:
                turn.reply, turn.folded = "", True
            turn.done.set()
        return answered

    def _answer_claims_interim(self) -> None:
        """A turn end answered its utterances: an interim still waiting
        to speak for them has nothing to add - the answer is spoken, in
        full, by the voice. Cancelled here, in the same callback that
        set the answer, so the timer cannot slip in between: measured
        2026-08-30 23:34:44Z, interim at .508 and turn end at .510, the
        same paragraph twice."""
        task = self._interim_task
        if task is None or task.done():
            return
        if all(t.done.is_set() for t in self._interim_for):
            task.cancel()
            self._interim_task = None
            self._interim_for = []

    async def _say_interim(self, text: str,
                           answering: list[_VoiceTurn] | None = None) -> None:
        await asyncio.sleep(self.INTERIM_AFTER_S)
        waiting = answering if answering is not None else self._turns
        if all(t.done.is_set() for t in waiting):
            return                      # the answer is here; say that instead
        if self.on_interim is None:
            return
        self.record("boss_interim", {"text": text[:500]})
        if self._conductor is not None:
            self._conductor.bus.emit(ObservabilityEvent(
                type="boss.interim_spoken", component="manager",
                manager_session_id=self.session_id,
                data={"text": text[:200]}))
        try:
            self.on_interim(text)
        except Exception:
            application_log("manager", "boss.interim_failed",
                            "could not hand the Boss's first sentence to the "
                            "voice", severity="warning", exc_info=True)

    def _tool_started(self, execution_id: str, tool: str, args: dict) -> None:
        self._executions[execution_id] = tool
        self._set_status("waiting_for_tool")
        self.record("tool_started", {
            "execution_id": execution_id, "tool": tool, "args": args,
            "input_summary": input_summary(tool, args)})

    def _tool_finished(self, execution_id: str, call: ToolCall, ok: bool) -> None:
        self._executions.pop(execution_id, None)
        if ok:
            self.record("tool_completed", {
                "execution_id": execution_id, "tool": call.tool,
                "output_summary": output_summary(call.tool, call.result),
                "result": call.result[:4000]})
            self._record_child_action(call)
        else:
            self.record("tool_failed", {
                "execution_id": execution_id, "tool": call.tool,
                "error": call.result[:500]})
        self._set_status("thinking")

    def _record_child_action(self, call: ToolCall) -> None:
        """A worker started, messaged, approved or denied: a first-class
        Boss action with the child's stable identity attached."""
        if call.tool not in CHILD_ACTIONS or self.session is None:
            return
        event_type, _ = CHILD_ACTIONS[call.tool]
        task_id = call.args.get("task_id", "")
        title = ""
        if call.tool == "create_task":
            try:
                data = json.loads(call.result)
                task_id = data.get("task_id", task_id)
                title = data.get("title", "")
            except ValueError:
                pass
            if task_id:
                subagent_id = f"sub_{task_id}"
                if subagent_id not in self.session.child_subagent_ids:
                    self.session.child_subagent_ids.append(subagent_id)
                    self.store.save(self.session)
                parent = getattr(self._conductor, "set_parent_boss", None)
                if parent is not None:
                    try:
                        parent(task_id, self.session.id)
                    except Exception:
                        application_log("manager", "boss.parent_link_failed",
                                        f"could not link {task_id}",
                                        severity="warning", exc_info=True)
                self.record(event_type, {"task_id": task_id,
                                         "subagent_id": subagent_id,
                                         "title": title})
            return
        if call.tool == "send_to_task":
            self.record(event_type, {"task_id": task_id,
                                     "subagent_id": f"sub_{task_id}",
                                     "message": str(call.args.get("message", ""))[:500]})
            return
        self.record(event_type, {"task_id": task_id,
                                 "subagent_id": f"sub_{task_id}",
                                 "decision": "approved"
                                 if call.tool == "approve_task_action" else "denied"})

    def record_supervisory(self, event) -> None:
        """A meaningful worker event reached the Boss (from the
        SupervisorInbox): a durable line in this timeline, so the reason
        the Boss said something is on the record next to what it said."""
        if self.session is None:
            return
        self.record("subagent_event_received", {
            "task_id": event.task_id, "subagent_id": event.subagent_id,
            "title": event.task_title, "type": event.type,
            "summary": event.summary[:300], "supervisory_id": event.event_id},
            trace_id=event.trace_id)

    def record_voice_said(self, text: str) -> None:
        """What the user heard from the voice, in the timeline between
        their words and the Boss's - so the record reads as the
        conversation the user actually had. Not typed anywhere."""
        if self.session is None:
            return
        self.record("voice_message", {"text": text[:2000]})

    # -- worker turns, pushed ------------------------------------------------------
    # A worker's summary is typed into the Boss whole. 300 cut answers
    # mid-sentence ("several arrived cut off", the Boss said); 1500 cut
    # at a sentence still cut - and the ceiling that matters is the one
    # on the message itself (agent_events.SUMMARY_CEILING). The Boss
    # reads a window; the user is shown a line, cut where it is shown.
    UPDATE_LABEL = {"completed": "finished a turn", "failed": "failed",
                    "approval_required": "is asking for approval",
                    "input_required": "is asking a question",
                    "unexpected_interruption": "stopped unexpectedly"}

    def is_child(self, subagent_id: str) -> bool:
        return self.session is not None and \
            subagent_id in self.session.child_subagent_ids

    def update_line(self, event) -> str:
        what = self.UPDATE_LABEL.get(event.type, event.type)
        summary = " ".join(str(event.summary or "").split())
        # Whose worker it is, said rather than filtered on: the user has
        # one Boss, and a worker an earlier Boss started (a new chat, an
        # older build) is still theirs to hear about.
        whose = "Your worker" if self.is_child(event.subagent_id) else \
            "Worker (started before this chat)"
        line = f"{whose} · {event.task_title or event.task_id} " \
               f"({event.task_id}) {what}"
        return f"{line}: {summary}" if summary else line

    def deliver_supervisory(self, event) -> bool:
        """Push one worker event into the Boss's session, now or when
        the current voice turn is over. Every visible worker's, not only
        this Boss's own children - measured: eleven Boss sessions in a
        day, and the workers of the previous ten were never mentioned to
        the current one. Returns whether the event was taken (pushed or
        queued)."""
        if not self.push_updates or self.session is None or \
                self.session_id is None:
            return False
        line = self.update_line(event)
        self._pushed_ids.append(event.event_id)
        # Now, whatever the Boss is doing: mid-turn, Claude Code keeps
        # the line in its box and reads it when it looks up, the same as
        # the user's own next words. Updates used to wait for every open
        # voice turn to end - measured 2026-08-30 08:47-08:51: three
        # finishes held for three minutes behind one turn that had hung
        # open. Claude Code queues typed lines itself, so a push does not
        # wait for a previous push's turn either; only the keyboard lock
        # serializes the typing, and whatever failed to type earlier goes
        # first, in one typed line with this one.
        self._pending_updates.append(line)
        loop = asyncio.get_event_loop()
        loop.create_task(self.flush_updates())
        return True

    def resync_updates(self) -> None:
        """Whatever the durable inbox still owes the Boss goes now: events
        a previous process queued and lost with it, or ones offered before
        this session was open. An event is acked only when the push that
        carries it lands, so nothing here is guessed - undelivered means
        owed, and a duplicate ack write lost to a crash costs at most one
        repeated line in the Boss's window."""
        if not self.push_updates:
            return
        inbox = getattr(self._conductor, "inbox", None)
        if inbox is None:
            return
        owed = [e for e in inbox.pending_for_manager()
                if e.event_id not in self._pushed_ids]
        if not owed:
            return
        application_log("manager", "boss.updates_resynced",
                        f"{len(owed)} worker update(s) still owed to the "
                        "Boss are being pushed", owed=len(owed))
        for event in owed:
            self._pushed_ids.append(event.event_id)
            self._pending_updates.append(self.update_line(event))
        asyncio.get_event_loop().create_task(self.flush_updates())

    # A push that could not be typed is tried again after these delays,
    # then for ever at PUSH_RETRY_CEILING_S: a worker's finish is owed to
    # the Boss until a push lands, however long its window is gone, and
    # a flush trigger (a turn ending, the next update) may never come.
    # Measured 22:31:25Z: "PTY is gone" twice during a dark wake, and
    # both updates - a finish among them - were logged and lost.
    PUSH_RETRY_S = (5.0, 15.0, 45.0)
    PUSH_RETRY_CEILING_S = 60.0

    def _finish_push(self) -> None:
        """The Boss ended a push's turn; anything a failed send left
        behind goes next."""
        self._pushes_open = max(0, self._pushes_open - 1)
        self._told_user_in_push = False
        if self._pending_updates:
            asyncio.get_event_loop().create_task(self.flush_updates())

    async def _push(self, lines: list[str]) -> None:
        if not lines or self.session_id is None:
            return
        # One typed line - cmux presses Enter for a newline (compose()
        # says why) - so several updates become one turn, " · " apart.
        text = " · ".join(lines)
        # A push reaches the Boss without _ensure_session, so it is the
        # other place a Boss nobody is reading would answer into the void.
        self._ensure_watched()
        self._pushes_open += 1
        self._push_texts.append(text)
        del self._push_texts[:-8]
        self._told_user_in_push = False
        lock = getattr(self, "_send_lock", None)
        if lock is None:
            lock = self._send_lock = asyncio.Lock()
        try:
            async with lock:            # one keyboard, shared with handle
                await self.runtime.send(self.session_id, text)
            self.record("system_event", {"text": f"pushed to the Boss: {text[:300]}",
                                         "kind": "worker_update"})
            # Delivered here, so it is not owed to the Boss in a digest
            # too. (The digest was the invisible Boss's channel.)
            inbox = getattr(self._conductor, "inbox", None)
            if inbox is not None and self._pushed_ids:
                inbox.ack_manager(self._pushed_ids)
                self._pushed_ids = []
        except Exception:
            self._pushes_open = max(0, self._pushes_open - 1)
            if text in self._push_texts:
                self._push_texts.remove(text)
            # Back at the front, in order: whatever queued meanwhile
            # follows it, and _pushed_ids are acked on the push that
            # lands, not this one.
            self._pending_updates[:0] = lines
            attempt = self._push_failures
            self._push_failures += 1
            application_log("manager", "boss.update_push_failed",
                            "could not type a worker update into the Boss; "
                            "kept to try again", severity="warning",
                            exc_info=True, attempt=attempt + 1,
                            pending=len(self._pending_updates))
            delay = (self.PUSH_RETRY_S[attempt]
                     if attempt < len(self.PUSH_RETRY_S)
                     else self.PUSH_RETRY_CEILING_S)
            loop = asyncio.get_event_loop()
            loop.call_later(delay,
                            lambda: loop.create_task(self._retry_push()))
            return
        self._push_failures = 0

    async def _retry_push(self) -> None:
        if not self._pending_updates:
            return
        if self.busy:
            # The turn in flight flushes what is pending when it ends;
            # if it never does, the retry comes back instead.
            loop = asyncio.get_event_loop()
            loop.call_later(self.PUSH_RETRY_CEILING_S,
                            lambda: loop.create_task(self._retry_push()))
            return
        application_log("manager", "boss.update_push_retry",
                        "trying a held worker update again",
                        pending=len(self._pending_updates))
        await self.flush_updates()

    async def flush_updates(self) -> None:
        pending, self._pending_updates = self._pending_updates, []
        await self._push(pending)

    async def _count_turn(self, session_id: str, conductor) -> None:
        """One more voice turn reached the Boss. On the one that makes
        this a conversation, its window is put in front of the user -
        once; after that the user decides what is in front."""
        self._voice_turns += 1
        if self._window_shown or self._voice_turns < self.SHOW_AFTER_TURNS:
            return
        self._window_shown = True
        bring = self.show_window \
            or getattr(self.runtime, "bring_forward", None)
        if bring is None:
            return
        try:
            # cmux calls are synchronous and slow; off the loop, like
            # every other one, so the utterance in flight is not stalled.
            if bring is self.show_window:
                await asyncio.to_thread(bring)
            else:
                await asyncio.to_thread(bring, session_id)
        except Exception:
            application_log("manager", "boss.window_show_failed",
                            "could not put the Boss's window in front",
                            severity="warning", exc_info=True)
            return
        conductor.bus.emit(ObservabilityEvent(
            type="boss.window_shown", component="manager",
            manager_session_id=session_id,
            data={"after_turns": self._voice_turns}))

    # -- one turn ------------------------------------------------------------------
    def compose(self, text: str, conductor) -> str:
        """What is typed into the Boss's window for one utterance: the
        user's words, and nothing else.

        The invisible Boss was handed a bundle - clock, capability
        snapshot, task registry, then "User says: ..." - in the user
        message, where nobody saw it. In a window it is the transcript
        the user reads, and it must show what they said. The bundle's
        parts live elsewhere now: capabilities in the session's
        instructions (static per run), worker state pushed as it happens
        and on demand through the tools, the date in Claude Code's own
        prompt.

        One line: a terminal input submits on Enter, and cmux presses
        Enter for every newline it is asked to type. Words are kept;
        line breaks become spaces.
        """
        return " ".join(text.split())

    async def _type_in(self, session_id: str, text: str,
                       conductor) -> _VoiceTurn:
        """The user's words into the window, now. Under the send lock."""
        from .observability import current_trace
        self._turn_trace = current_trace()
        # The voice side attaches the user's verbatim words under a
        # header, for a Boss that reads its prompt off a wire. This Boss's
        # prompt is a window the user reads back: it gets their words and
        # nothing else. The frontend's summary is kept in the timeline.
        summary, spoken = users_words(text)
        text = spoken or summary
        self.record("user_message", {"text": text, "source": "voice",
                                     **({"frontend_summary": summary}
                                        if spoken else {})})
        if self._bridge is not None and not self._turns:
            self._bridge.drain()        # stale calls; a turn in flight keeps its own
        turn = _VoiceTurn(text=text, started=time.monotonic())
        # Listed before the keys go in: the session can report the words
        # landing - or answer them - before send returns.
        self._turns.append(turn)
        self._interim_said = False
        self._set_status("thinking")
        if len(self._turns) > 1:
            conductor.bus.emit(ObservabilityEvent(
                type="boss.words_queued", component="manager",
                manager_session_id=session_id,
                data={"text": text[:300], "open_turns": len(self._turns)}))
        try:
            await self.runtime.send(session_id, self.compose(text, conductor))
        except BaseException:
            self._turns.remove(turn)
            raise
        await self._count_turn(session_id, conductor)
        return turn

    async def _await_answer(self, session_id: str, turn: _VoiceTurn,
                            conductor) -> ManagerTurn:
        """The Boss's answer to one utterance, once it has gone quiet."""
        deadline = turn.started + self.turn_timeout
        try:
            try:
                await asyncio.wait_for(turn.done.wait(),
                                       max(deadline - time.monotonic(), 0.0))
                # A turn end is not always the end: the Boss can say a
                # sentence, call more tools, and answer properly. Take the
                # answer only once the session has gone quiet - unless it
                # has moved on to the next thing the user said.
                while (not turn.closed
                       and time.monotonic() - self._last_event_at < self.SETTLE_S
                       and time.monotonic() < deadline):
                    await asyncio.sleep(0.25)
            except asyncio.TimeoutError:
                application_log("manager", "manager.turn_timeout",
                                "the Boss did not finish its turn in time",
                                severity="warning", timeout_s=self.turn_timeout)
                turn.reply = turn.reply or "That is still in progress."
                self.record("system_event", {"text": "turn timed out; the "
                                                     "answer will follow"})
        finally:
            if turn in self._turns:
                self._turns.remove(turn)
            if not self._turns:
                if self._interim_task is not None and not self._interim_task.done():
                    self._interim_task.cancel()
                self._interim_task = None
                self._interim_for = []
        calls = self._bridge.drain() if self._bridge is not None else []
        if self._pending_updates:
            await self.flush_updates()
        conductor.bus.emit(ObservabilityEvent(
            type="boss.turn", component="manager",
            manager_session_id=session_id,
            duration_ms=round((time.monotonic() - turn.started) * 1000, 1),
            data={"boss_session_id": self.session.id if self.session else "",
                  "tools": [c.tool for c in calls],
                  "reply": turn.reply[:300], "folded": turn.folded}))
        return ManagerTurn(reply=" ".join(turn.reply.split()),
                           tool_calls=calls, folded=turn.folded)
