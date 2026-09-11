"""TmuxClaudeRuntime: the execution lives in an attachable PTY.

The interactive answer to "one task, one execution": each task's worker is a
real interactive `claude` process inside a tmux session. The user attaches a
terminal to that PTY and can watch AND type; the Conductor drives the same
process (tmux send-keys) and observes it structurally by tailing the session
JSONL Claude Code itself writes (~/.claude/projects/<munged cwd>/<id>.jsonl)
- the provider's own record, not screen scraping.

    Managed Subagent
          ↓
    interactive claude in tmux  ←— user attaches, watches, types
          ↓
    session JSONL  →  AgentEvents  →  Conductor/Manager

Trade-offs versus the SDK runtime, stated honestly:
  - permission prompts render in the pane for the USER to answer; the
    conductor-side approval tools do not apply here
  - turn completion is derived from the provider's stop_reason records
  - tool policy is coarser (permission mode, not per-tool callbacks)
Use it when interactivity matters most; the SDK runtime remains the
structured default.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import select
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import agent_feed, instance
from .agent_events import AgentEvent
# What the runtime knows about the CLI it hosts lives in the adapter now
# (docs/any-cli.md). The Claude Code names this module used to define
# are re-exported from here so their importers do not notice the move.
from .cli_adapter import (CLAUDE_PROJECTS, PROMPT_READY,  # noqa: F401
                          ClaudeCodeAdapter, CliAdapter,
                          detect_approval_prompt, munge_project_dir,
                          normalize_entry)
from .observability import (ObservabilityBus, ObservabilityEvent,
                            application_log)
from .runtime import (ApprovalPolicy, CodingAgentRuntime, EventHandler,
                      ExecutionTranscript, TaskExecution)

# What a worker must NOT inherit, whoever hosts its PTY.
#
# A worker is started by whatever started the conductor, and if that was a
# Claude Code session the child markers come with it. Claude Code sees
# CLAUDE_CODE_CHILD_SESSION and turns transcript saving OFF - the file
# every piece of our supervision reads. No turns, no completions, no
# approvals, and _discover_session_file waits out the whole startup
# timeout for a session file that is never going to be written, then kills
# the pane and raises. The user asked for a worker and got no window at
# all.
#
# The stale API key goes for the same reason it goes everywhere else: it
# takes the API path and 401s.
#
# This lived only in the cmux runtime, so the tmux path - the fallback for
# a machine without cmux, and the one you get when a Claude Code session
# launches the conductor for you - was the one that broke.
CHILD_MARKERS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_CHILD_SESSION",
                 "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
                 "CLAUDE_CODE_BRIDGE_SESSION_ID")

# Secrets only the conductor itself uses. The OpenAI key is the voice's,
# read from .env; a worker has no use for it and could read it with `env`.
# conduct.py takes it out of its own environment once read, but that is
# not enough on its own: a pane gets tmux's GLOBAL environment, which is
# the environment of whatever started the server, and the server outlives
# conductors (measured 2026-09-10: `tmux show-environment -g` held it).
# Not in CHILD_MARKERS, because conduct.sh strips those and the conductor
# needs this one.
CONDUCTOR_SECRETS = ("OPENAI_API_KEY",)


def scrub_argv(task_id: str | None = None,
               home: str | Path | None = None) -> list[str]:
    """`env -u ...` prefix that strips CHILD_MARKERS and CONDUCTOR_SECRETS
    from a worker.

    It also NAMES the worker. A task allowed to drive the GUI reaches
    conductor/computer.py through Bash, as a fresh process with nothing
    of the conversation in it, so the only way it can say which task it
    is - and therefore whether a live conductor still holds its lease -
    is the environment it was launched with. See conductor/instance.py.
    """
    argv = ["env"]
    for name in CHILD_MARKERS + CONDUCTOR_SECRETS:
        argv += ["-u", name]
    if task_id:
        argv.append(f"{instance.TASK_ENV}={task_id}")
    if home:
        argv.append(f"{instance.HOME_ENV}={home}")
    return argv




@dataclass
class _TmuxSession:
    task_id: str
    name: str                     # tmux session name == the PTY handle
    working_directory: str
    session_id: str | None = None
    jsonl_path: Path | None = None
    offset: int = 0
    status: str = "running"
    handlers: list[EventHandler] = field(default_factory=list)
    watcher: asyncio.Task | None = None
    state: dict = field(default_factory=dict)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    pending_approval: dict | None = None   # the one prompt a TUI can show
    sequence: int = 0             # last event number stamped for this execution


class _TranscriptWakeup:
    """Wakes a watcher the moment its transcript changes.

    kqueue's EVFILT_VNODE fires on the write itself, so a turn end is
    read as it lands instead of on the next poll. The poll interval is
    still the ceiling on a wait - liveness and the approval prompt are
    checked at least that often - and where kqueue or the transcript is
    unavailable (no file yet, a screen-only session, another platform)
    the wait is a plain sleep.
    """

    def __init__(self) -> None:
        self._path: Path | None = None
        self._fd = -1
        self._kq = None

    def _arm(self, path: Path | None) -> bool:
        if path != self._path:
            self.close()
            self._path = path
        if self._kq is not None:
            return True
        if path is None or not hasattr(select, "kqueue"):
            return False
        try:
            self._fd = os.open(path, os.O_RDONLY)
            self._kq = select.kqueue()
            self._kq.control([select.kevent(
                self._fd, filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=(select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND |
                        select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME))],
                0, 0)
            return True
        except OSError:
            self.close()
            return False

    def wait(self, path: Path | None, timeout: float) -> None:
        """Block until the transcript changes or the timeout passes.
        Blocking: run it off the event loop. Writes that land while the
        watcher is reading are kept by the kernel and returned on the
        next call, so nothing slips between a read and a wait."""
        if not self._arm(path):
            time.sleep(timeout)
            return
        try:
            events = self._kq.control(None, 1, timeout)
        except OSError:
            self.close()
            return
        for event in events:
            if event.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                self.close()          # the file moved; re-open next wait

    def close(self) -> None:
        if self._kq is not None:
            try:
                self._kq.close()
            except OSError:
                pass
            self._kq = None
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1


# What a pane looks like when it is waiting on the user. The shape of a
# choice rather than any particular wording: the point is that
# something is asking, not what it asked.
_PROMPT_SHAPES = ("do you want", "would you like", "1. yes", "2. no",
                  "no, and tell claude", "(y/n)", "esc to reject",
                  "permission to")


def choose_option(pane: str, wanted: str) -> int:
    """How many Downs to reach the option containing `wanted`.

    Answering a dialog by pressing Enter assumes the option you want is the
    one already selected. That held in a tmux pane, where the trust prompt
    offers "Yes, I trust this folder" first - and does not hold in a cmux
    surface, where the same dialog puts "No, exit" first. The watchdog
    written to rescue stuck workers would have exited them.

    So find the line, count from the marked one, and press that many times.
    Returns -1 when the wanted option is not on screen, which means answer
    nothing rather than answer blind.
    """
    # Box-drawing furniture is not part of the option: Gemini draws its
    # dialog inside a frame ("\u2502 \u25cf 1. Trust folder (x)") and marks the
    # selected line with "\u25cf"; Codex with "\u203a"; Claude Code with "\u276f".
    lines = [ln.strip(" \t\u2502\u2503\u2551\u256d\u2570\u256e\u256f\u2500\u2550") for ln in pane.splitlines()]
    lines = [ln for ln in lines if ln]
    options, selected, target = [], -1, -1
    for stripped in lines:
        # A yes/no line, or any numbered choice ("3. Continue without
        # trusting" - Codex's hooks dialog).
        is_option = bool(re.match(r"^[\u276f>*\u203a\u25cf\u25c9]?\s*(\d+\.\s*|\d*\.?\s*(yes|no)\b)",
                                  stripped, re.I))
        if not is_option:
            continue
        if wanted.lower() in stripped.lower():
            target = len(options)
        if stripped.startswith(("\u276f", ">", "*", "\u203a", "\u25cf", "\u25c9")):
            selected = len(options)
        options.append(stripped)
    if target < 0:
        return -1
    if selected < 0:
        selected = 0              # nothing marked: assume the first
    return target - selected


def _prompt_in(pane: str) -> str:
    """The decision a pane is waiting on, or "" if it is not waiting."""
    low = pane.lower()
    if not any(shape in low for shape in _PROMPT_SHAPES):
        return ""
    # A trust prompt or resume picker is a boot dialog, not a decision -
    # unstick answers those, and reporting one would ask the user to
    # approve something the system already handles.
    if "trust this folder" in low or "resume from summary" in low \
            or "is this a project you" in low:
        return ""
    lines = [ln.strip() for ln in pane.splitlines() if ln.strip()]
    for index, line in enumerate(lines):
        if any(shape in line.lower() for shape in _PROMPT_SHAPES):
            return " ".join(lines[max(0, index - 3):index + 1])[:300]
    return ""


# What an EMPTY input box looks like, measured off a live worker: the
# prompt character followed by U+00A0. Anything else on that line is the
# user's, half-written.
# macOS pgrep leaves out its own ancestors unless told otherwise (-a). A
# process-table question asked from inside a worker - a probe, a test, a
# conductor started from a worker's shell before that shell has gone -
# would then read that worker as having no process, and the sweep ends
# what it cannot see. Measured 2026-08-30: `pgrep -f claude` run under
# task_bd44c4cd listed every claude on the machine except that one.
PGREP = ["pgrep", "-a", "-f"] if sys.platform == "darwin" else ["pgrep", "-f"]

_PROMPT_CHARS = ("\u276f", ">")
# Text that SITS in the input box without anyone having typed it: the
# provider's own hint, and - measured - the note Claude Code leaves after
# it queues a message while working. Reading that note as "the user is
# mid-sentence" would hold back every follow-up after the first one.
_PLACEHOLDERS = ("try \"", "try '", "press up to edit queued messages")
# One character typed into the box and taken straight back out, to ask
# whether what is showing there was typed or merely suggested. See
# TmuxClaudeRuntime._ghost_in_the_box.
PROBE = "~"


def session_name(task_id: str) -> str:
    """The one name a task's worker is known by.

    Both sides have to agree on this or they cannot find each other, and
    they did not: the runtime named workspaces cond_<task_id> while the
    surface looked for cond_task_<task_id>. Task ids already begin with
    "task_", so the surface's name was cond_task_task_<id> and matched
    nothing, ever. Every click therefore resumed a duplicate worker
    instead of attaching to the one already running.
    """
    return f"cond_{task_id}"


# Where a session's last screen outlives it: written by destroy, read by
# the Boss window's terminal view (app_web), so a finished worker's
# card still shows what it did rather than "The session has ended." over
# nothing (measured 2026-09-01).
TERMINALS_DIR = Path.home() / ".voice-conductor" / "terminals"


def final_screen_path(name: str) -> Path:
    return TERMINALS_DIR / f"{name}.txt"


STREAMS_DIR = TERMINALS_DIR / "streams"


def stream_path(name: str) -> Path:
    """The pane's raw output, piped from launch (pipe-pane).

    tmux normalizes a full-screen CLI's drawing into grid repaints, so
    no tmux client can scroll what the program never scrolls (measured
    2026-09-01: history 0 while iTerm banks the same session's
    scrollback). This file is what a real terminal would have been fed
    - and what a real emulator (the window's xterm.js) can therefore
    scroll, the way cmux's embedded Ghostty does."""
    return STREAMS_DIR / f"{name}.raw"


def _typed_but_unsent(pane: str) -> str:
    """What the USER has half-written in the input box, or "".

    Two writers share one keyboard: the person looking at the workspace,
    and the Manager delivering a follow-up. send-keys appends to whatever
    is already in the box and then presses Enter, so a message arriving
    while someone is mid-sentence does not interleave harmlessly - it
    submits their unfinished words welded to ours, as one prompt neither
    of us wrote.

    The person wins. They are typing right now; the follow-up can wait for
    the box to clear.
    """
    for line in reversed([l for l in pane.splitlines() if l.strip()]):
        stripped = line.strip()
        if not stripped.startswith(_PROMPT_CHARS):
            continue
        rest = stripped[1:].replace("\xa0", " ").strip()
        if not rest:
            return ""
        if rest.lower().startswith(_PLACEHOLDERS):
            return ""             # the provider's own hint text, not input
        return rest[:200]
    return ""


class DuplicateSession(RuntimeError):
    """Two things want one session name, which is two workers on one
    conversation. Its own type so callers can tell it from the generic
    "could not resume" it used to hide inside."""


class TmuxClaudeRuntime(CodingAgentRuntime):
    def __init__(self, bus: ObservabilityBus | None = None,
                 transcript_dir: str | None = None,
                 claude_binary: str | None = None,
                 startup_timeout: float = 60.0,
                 approval_policy: ApprovalPolicy | None = None,
                 permission_mode: str = "auto",
                 adapter: CliAdapter | None = None) -> None:
        # One mode, always. The whole point of a delegated task is that
        # nobody is watching its terminal, and a worker that stops for every
        # edit is a worker that never finishes: the read-only mode this used
        # to fall back to made every edit prompt in a pane no one was
        # looking at, which read as "bypass is broken" rather than "a flag
        # is missing".
        # "auto" rather than "bypassPermissions": ordinary work proceeds,
        # and what the provider judges consequential still gets weighed
        # instead of waved through.
        self.permission_mode = permission_mode
        # This process's counting of event sequences. Stamped on every
        # event, so the reducer can tell a restarted runtime's 1 from a
        # late event in the old count (AgentEvent.epoch says why).
        self.epoch = "ep_" + secrets.token_hex(4)
        self.approval_policy = approval_policy or ApprovalPolicy()
        self.bus = bus or ObservabilityBus()
        self.transcript = ExecutionTranscript(transcript_dir) \
            if transcript_dir else None
        self.claude = claude_binary or shutil.which("claude") or "claude"
        # Which CLI this runtime hosts, and everything it knows about it:
        # launch flags, the ready prompt, the transcript, the dialogs.
        # Claude Code unless told otherwise; the hosting is the same.
        self._adapter = adapter or ClaudeCodeAdapter(self.claude)
        self.startup_timeout = startup_timeout
        if shutil.which("tmux") is None:
            raise RuntimeError("tmux is required for the interactive "
                               "runtime - `brew install tmux`")
        self.sessions: dict[str, _TmuxSession] = {}
        # The conductor home, so a worker is launched knowing where the
        # instance lock and the GUI lease live. Derived from the
        # transcript directory (<home>/executions), which is the only
        # thing this runtime is told about the home.
        if transcript_dir:
            self.home = str(Path(transcript_dir).expanduser().parent)

    # Where this conductor keeps its state. A class attribute, because
    # half the suite builds runtimes with __new__ and never runs
    # __init__; an unset home simply means "the default one".
    home: str | None = None

    # Which CLI this runtime hosts. A property with a default, not a
    # plain attribute: half the suite builds runtimes with __new__ and
    # never runs __init__, and a runtime that has not been told otherwise
    # hosts Claude Code.
    _adapter: CliAdapter | None = None

    @property
    def adapter(self) -> CliAdapter:
        if self._adapter is None:
            self._adapter = ClaudeCodeAdapter(getattr(self, "claude", None))
        return self._adapter

    @adapter.setter
    def adapter(self, value: CliAdapter) -> None:
        self._adapter = value

    @property
    def provider(self) -> str:
        """The one CLI this runtime drives, by its provider name - what
        the capability probe asks a single runtime."""
        return self.adapter.name

    # -- tmux helpers -------------------------------------------------------
    @staticmethod
    def _tmux(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", *args], capture_output=True,
                              text=True)

    @staticmethod
    def _send_argv(name: str, message: str) -> list[list[str]]:
        """send-keys argv pairs: the literal text, then Enter. -l keeps tmux
        from interpreting the message as key names.

        Kept for callers that want the argv; delivery goes through _tmux so
        that every pane operation has ONE seam. It did not, and a subclass
        hosting workers elsewhere had its follow-ups sent to real tmux -
        which reported a pane it had never heard of."""
        return [["send-keys", "-t", name, "-l", message],
                ["send-keys", "-t", name, "Enter"]]

    def _alive(self, name: str) -> bool:
        return self._tmux("has-session", "-t", name).returncode == 0

    def session_alive(self, name: str) -> bool:
        """A second opinion on one PTY, for the sweep.

        Asked directly, by name, the way send and focus ask - not read off
        a listing. A listing is one command's output format away from
        naming nobody, and "nobody is alive" is the one answer the sweep
        acts on without appeal: it interrupts every running task, and the
        Manager then treats each of them as abandoned and starts a fresh
        one for every follow-up.
        """
        return self._alive(name)

    def worker_process_alive(self, name: str, working_directory: str | None,
                             session_id: str | None) -> bool:
        """Is a worker's process running, whatever its window says?

        The sweep's last word on a session the host still lists. Under
        cmux a workspace outlives the process inside it: measured
        2026-08-30, claude in cond_task_1c2db9f0 exited at 09:09:25Z in
        a Conductor restart, the workspace stayed up as a bare shell,
        has-session said alive, and the task was counted as a busy worker
        for the next forty minutes - one of three slots, so create_task
        refused every new worker with "3 workers are busy", and each
        send_to_task to it waited 45 s for a prompt an empty shell never
        shows. Cannot tell reads as alive, as _process_alive does.
        """
        sess = self.sessions.get(session_id) if session_id else None
        if sess is None:
            sess = next((s for s in self.sessions.values() if s.name == name),
                        None)
        if sess is None:
            if not working_directory:
                return True           # nothing to look for: no opinion
            sess = _TmuxSession(task_id=name, name=name,
                                working_directory=working_directory,
                                session_id=session_id)
        return self._process_alive(sess)

    def _process_alive(self, sess: _TmuxSession) -> bool:
        """The last word before a session is declared ended: is its
        claude process running, whatever the host says?

        has-session asks the host - tmux, or under cmux a workspace
        listing - and the host can be wrong in a way the process table
        cannot: a listing that fails names nobody. A session known by id
        is on its own command line (--session-id, --resume); a fresh
        worker is not, and is found by the one thing that names it - its
        checkout, which no other claude runs in. Cannot tell reads as
        alive: ending a live session costs its report, ending a dead one
        a few seconds later costs nothing.
        """
        try:
            needle = self.adapter.process_needle(sess.session_id)
            if needle:
                found = subprocess.run([*PGREP, needle],
                                       capture_output=True, text=True,
                                       timeout=5)
                if found.stdout.strip():
                    return True
            found = subprocess.run([*PGREP, self.adapter.binary_name
                                    or os.path.basename(self.adapter.binary)],
                                   capture_output=True, text=True, timeout=5)
            pids = found.stdout.split()
            if not pids:
                return False
            cwds = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn",
                                   "-p", ",".join(pids)],
                                  capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return True
        wanted = os.path.normpath(sess.working_directory)
        return any(os.path.normpath(line[1:]) == wanted
                   for line in cwds.stdout.splitlines()
                   if line.startswith("n"))

    async def _off_loop(self, fn, *args):
        """Run one PTY call where it cannot stall the event loop.

        Every _tmux call is a subprocess, and under cmux each one is a
        socket round trip of a few hundred milliseconds - or twenty
        seconds, when _await_prompt waits for a shell that never shows
        the prompt it expects. Measured live: create_task held the loop
        for 23 s, the user's Fn press inside that window was processed
        afterwards as a 1 ms blip, and the microphone thread (which
        reads the hold flag) sent silence for the whole utterance. The
        agent "did not hear the second thing". The loop must keep
        turning while a pane is being driven.
        """
        return await asyncio.to_thread(fn, *args)

    async def _pane(self, name: str) -> str:
        """The pane's text, captured off the loop."""
        done = await self._off_loop(self._tmux, "capture-pane", "-t", name,
                                    "-p")
        return done.stdout

    def _emit(self, sess: _TmuxSession, event: AgentEvent) -> None:
        # Stamp the position within this execution, once, here: the one
        # seam every event of this runtime passes. The reducer uses it to
        # refuse a late event that would rewind state.
        if not event.sequence:
            sess.sequence += 1
            event = replace(event, sequence=sess.sequence)
        if not event.epoch:
            event = replace(event, epoch=getattr(self, "epoch", ""))
        for handler in list(sess.handlers):
            try:
                handler(event)
            except Exception:
                application_log(
                    "runtime", "runtime.event_handler_failed",
                    f"task event handler failed for {event.type}",
                    severity="error", exc_info=True, task_id=sess.task_id,
                    provider_session_id=sess.session_id,
                    agent_event=event.type,
                    handler=getattr(handler, "__qualname__", repr(handler)))

    def _log(self, sess: _TmuxSession, text: str) -> None:
        if self.transcript is not None:
            self.transcript.write(sess.task_id, text)

    def _observe_approval(self, sess: _TmuxSession, event_type: str,
                          **data) -> None:
        self.bus.emit(ObservabilityEvent(
            type=event_type, component="runtime", task_id=sess.task_id,
            provider_session_id=sess.session_id, data=data))

    def _check_approval_prompt(self, sess: _TmuxSession,
                               pane: str | None = None) -> None:
        """The approval lifecycle for a TUI-hosted worker, owned here.

        Detection: the pane sits on a permission prompt -> structured
        approval_required, status waiting_for_approval (never 'stuck').
        Policy: routine actions are answered automatically through this
        exact PTY. Resolution from either side: if the user answers the
        prompt directly in the pane, the vanished prompt IS the provider
        acknowledgement - pending clears and the Manager sees working.

        `pane` is the capture to judge; the async callers take it off the
        loop first, a sync caller lets this read it.
        """
        if pane is None:
            pane = self._tmux("capture-pane", "-t", sess.name, "-p").stdout
        description = self.adapter.approval_prompt(pane)

        if description is None:
            if sess.pending_approval is not None and \
                    not sess.state.get("deciding"):
                # Gate cleared without our decision: the user resolved it
                # directly in the session (or the provider invalidated it).
                approval = sess.pending_approval
                sess.pending_approval = None
                sess.status = "running"
                self._emit(sess, AgentEvent(
                    type="approval_resolved",
                    detail={"approval_id": approval["approval_id"],
                            "decision": "resolved",
                            "resolved_by": "user_direct"}))
                self._observe_approval(sess, "approval.resolved",
                                       approval_id=approval["approval_id"],
                                       resolved_by="user_direct")
            return

        if sess.pending_approval is not None:
            return                    # already tracked; still waiting

        approval_id = "appr_" + __import__("secrets").token_hex(4)
        approval = {"approval_id": approval_id,
                    "description": description[:300]}
        self._observe_approval(sess, "approval.detected",
                               approval_id=approval_id,
                               description=description[:200])
        # Policy by prompt kind: repo reads/searches and ordinary edits are
        # routine - answered automatically through this same PTY, logged,
        # and never surfaced as a blocking question. Only genuinely
        # consequential or unrecognized prompts escalate. Workers always
        # write, so the write policy always applies.
        decision = self.approval_policy.decide_prompt(
            description, allow_write=True)
        if decision == "allow":
            self._tmux("send-keys", "-t", sess.name, "1")
            self._tmux("send-keys", "-t", sess.name, "Enter")
            self._observe_approval(sess, "approval.policy_decision",
                                   approval_id=approval_id,
                                   decision="allow",
                                   description=description[:200])
            return                    # never entered waiting state
        sess.pending_approval = approval
        sess.status = "waiting_for_approval"
        self._emit(sess, AgentEvent(type="approval_required",
                                    question=description[:300],
                                    detail={"approval": approval}))
        self._observe_approval(sess, "approval.user_requested",
                               approval_id=approval_id)

    # -- session jsonl watching ------------------------------------------------
    # How a watcher decides its session is gone. One "no" from has-session
    # is not enough: under cmux that answer is a workspace listing, and a
    # listing that fails or times out reads as "nobody" for as long as it
    # is cached (WORKSPACES_TTL_S, one second). Measured 2026-08-29, twice
    # in one run (15:16:58Z, 22:30:55Z): every watched session - the
    # workers and the Boss - was declared "tmux session ended" in the same
    # second while every process was alive; the tasks went terminal, the
    # workers' reports were dropped, and the Boss was never read again.
    # So the session has to be missing on several consecutive polls that
    # together outlast the cache, and a transcript that is still growing
    # is proof of life whatever the listing says.
    #
    # The poll interval is the ceiling on a wait, not the pace of turn-end
    # detection: _TranscriptWakeup wakes the watcher the moment the
    # transcript is written where kqueue is available.
    WATCH_POLL_S = 0.3
    WATCH_GONE_MISSES = 3
    WATCH_GONE_AFTER_S = 3.0

    async def _watch(self, sess: _TmuxSession) -> None:
        missing_since: float | None = None
        vouched_at: float | None = None
        misses = 0
        why = "cancelled"
        wakeup = _TranscriptWakeup()
        try:
            while True:
                await self._off_loop(wakeup.wait, sess.jsonl_path,
                                     self.WATCH_POLL_S)
                try:
                    grew = await self._watch_once(sess)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Reading the pane or the transcript failed. That is a
                    # problem to report, not a reason to stop watching a
                    # session that is still there - and reported once per
                    # streak, because this loop runs three times a second.
                    if not sess.state.get("watch_crashing"):
                        sess.state["watch_crashing"] = True
                        application_log(
                            "runtime", "runtime.watch_failed",
                            "session watcher failed; it keeps watching, "
                            "and logs again once it recovers",
                            severity="error", exc_info=True,
                            task_id=sess.task_id,
                            provider_session_id=sess.session_id)
                    continue
                if sess.state.pop("watch_crashing", None):
                    application_log("runtime", "runtime.watch_recovered",
                                    "session watcher recovered",
                                    task_id=sess.task_id,
                                    provider_session_id=sess.session_id)
                if sess.state.get("watch_alive") or grew:
                    if missing_since is not None:
                        application_log(
                            "runtime", "runtime.session_found",
                            f"{sess.name} answered again after "
                            f"{misses} missed polls",
                            task_id=sess.task_id,
                            provider_session_id=sess.session_id,
                            misses=misses)
                    missing_since, vouched_at, misses = None, None, 0
                    continue
                now = time.monotonic()
                misses += 1
                if missing_since is None:
                    missing_since = now
                    application_log(
                        "runtime", "runtime.session_missing",
                        f"{sess.name} did not answer has-session; "
                        "confirming before declaring it ended",
                        severity="warning", task_id=sess.task_id,
                        provider_session_id=sess.session_id)
                if misses < self.WATCH_GONE_MISSES or \
                        now - missing_since < self.WATCH_GONE_AFTER_S:
                    continue
                # Missing for long enough by the host's account. The
                # process table gets the last word - asked once per
                # threshold, not per poll, because it is two subprocesses.
                if vouched_at is not None and \
                        now - vouched_at < self.WATCH_GONE_AFTER_S:
                    continue
                if await self._off_loop(self._process_alive, sess):
                    if vouched_at is None:
                        application_log(
                            "runtime", "runtime.session_process_alive",
                            f"{sess.name} is not listed by the host but "
                            "its claude process is running; still watching",
                            severity="warning", task_id=sess.task_id,
                            provider_session_id=sess.session_id,
                            misses=misses)
                    vouched_at = now
                    continue
                if sess.status != "disconnected":
                    sess.status = "disconnected"
                    self._emit(sess, AgentEvent(
                        type="failed", error="tmux session ended"))
                why = f"session ended after {misses} missed polls"
                return
        except asyncio.CancelledError:
            raise
        finally:
            wakeup.close()
            application_log("runtime", "runtime.watch_ended",
                            f"stopped watching {sess.name}: {why}",
                            task_id=sess.task_id,
                            provider_session_id=sess.session_id,
                            status=sess.status)

    async def _watch_once(self, sess: _TmuxSession) -> bool:
        """One poll: liveness, the approval prompt, the transcript.

        Returns whether the transcript grew. Leaves the liveness answer in
        sess.state["watch_alive"] for _watch to weigh."""
        alive = await self._off_loop(self._alive, sess.name)
        sess.state["watch_alive"] = alive
        pane = None
        if alive:
            try:
                pane = await self._pane(sess.name)
                self._check_approval_prompt(sess, pane)
                sess.state.pop("watch_failing", None)
            except Exception:
                # This loop runs three times a second: report the
                # start of a failure streak, not every iteration.
                if not sess.state.get("watch_failing"):
                    sess.state["watch_failing"] = True
                    application_log(
                        "runtime", "runtime.approval_watch_failed",
                        "approval prompt watcher failed; further "
                        "failures are logged once it recovers",
                        severity="error", exc_info=True,
                        task_id=sess.task_id,
                        provider_session_id=sess.session_id)
        if pane is not None and self.adapter.startup_dialog(pane) is not None:
            # A boot dialog after adoption: a CLI that asks for trust or
            # sign-in once its prompt was already seen, or a sign-in that
            # nobody can answer. Answer what can be answered; and a
            # sign-in becomes the user's question here, on the watcher,
            # where the task is subscribed - emitted during discovery it
            # reached no one. Measured: a Gemini worker showed Working for
            # four minutes on its sign-in screen.
            self._handle_startup_prompts(sess, pane)
            if self.adapter.startup_dialog(pane) == "auth" and \
                    not sess.state.get("auth_asked"):
                sess.state["auth_asked"] = True
                sess.status = "running"
                self._emit(sess, AgentEvent(
                    type="needs_input",
                    question=f"{self.adapter.display} needs you to sign in - "
                             "open its window"))
            return False
        if sess.jsonl_path is None:
            # No transcript to tail: the screen is the transcript. The
            # adapter reads turn ends and text off it (ScreenAdapter);
            # an adapter without that answers nothing, and the session
            # is followed for liveness and approvals only.
            if pane is None or sess.pending_approval is not None:
                return False
            grew = False
            for event in self.adapter.screen_events(pane, sess.state):
                grew = True
                if event.type == "completed":
                    sess.status = "idle"
                    self._log(sess, "\n✓ turn complete\n")
                else:
                    sess.status = "running"
                    self._log(sess, event.summary)
                self._emit(sess, event)
            return grew
        if not sess.jsonl_path.exists():
            return False
        data = sess.jsonl_path.read_text()
        new = data[sess.offset:]
        sess.offset = len(data)
        grew = bool(new.strip())
        for line in new.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                application_log(
                    "runtime", "runtime.provider_json_invalid",
                    "could not decode provider session entry",
                    severity="warning", task_id=sess.task_id,
                    provider_session_id=sess.session_id,
                    path=str(sess.jsonl_path), line=line[:300])
                continue
            for event in self.adapter.normalize(entry, sess.state):
                if event.type == "completed":
                    sess.status = "idle"
                    self._log(sess, "\n✓ turn complete\n")
                else:
                    sess.status = "running"
                    self._log(sess, event.summary)
                self._emit(sess, event)
        return grew

    def _answer_choice(self, name: str, pane: str, wanted: str) -> bool:
        """Move to the option we mean, then confirm.

        Never a bare Enter: the position of "yes" is not stable across
        terminals, and pressing it blind is how the trust dialog becomes an
        exit.
        """
        steps = choose_option(pane, wanted)
        if steps < 0:
            return False              # not on screen; answer nothing
        for _ in range(steps):
            self._tmux("send-keys", "-t", name, "Down")
        for _ in range(-steps if steps < 0 else 0):
            self._tmux("send-keys", "-t", name, "Up")
        self._tmux("send-keys", "-t", name, "Enter")
        return True

    def _handle_startup_prompts(self, sess: _TmuxSession,
                                pane: str | None = None) -> None:
        """Answer the dialogs Claude Code shows before it will take input.

        Two of them, and either will hold a session at its boot screen
        indefinitely because nobody is watching that window:

        The folder-trust dialog. Task workspaces are conductor-created
        worktrees of projects the user registered - the same directories
        headless workers already run in - so trusting them here is the
        established policy, not an escalation.

        The resume picker, shown by `claude --resume`. This is what left
        woken sessions stuck: the pane sat on "Resume from summary
        (recommended)" forever, the input prompt never appeared, and every
        message sent to that worker timed out. Take the recommended default,
        which is what a person would press.
        """
        if pane is None:
            pane = self._tmux("capture-pane", "-t", sess.name, "-p").stdout
        dialog = self.adapter.startup_dialog(pane)
        if dialog == "chrome":
            self._tmux("send-keys", "-t", sess.name, "Escape")
        elif dialog == "trust":
            # Claude Code offers "Yes, I trust this folder"; Codex and
            # Gemini offer "Yes, continue" / "Trust folder". Whichever
            # is on screen; a bare Enter when the marked option is it.
            if not self._answer_choice(sess.name, pane, "trust this folder"):
                if not self._answer_choice(sess.name, pane, "trust folder"):
                    self._answer_choice(sess.name, pane, "yes")
        elif dialog == "bypass":
            # Claude Code's one-time Bypass Permissions acceptance. The
            # session was launched with that mode on purpose, and the
            # dialog's selected option is "No, exit" - taken by a bare
            # Enter, it closes the session with nothing on screen saying
            # why (the Boss then has no tools and every turn fails).
            if self._answer_choice(sess.name, pane, "yes, i accept"):
                application_log("runtime", "runtime.bypass_prompt_accepted",
                                "accepted Claude Code's bypass-permissions "
                                "dialog", task_id=sess.task_id)
        elif dialog == "hooks":
            # A checkout we made has hooks we did not review; they stay
            # off. (Codex: "3. Continue without trusting".)
            self._answer_choice(sess.name, pane, "without trusting")
        elif dialog == "resume_picker":
            self._tmux("send-keys", "-t", sess.name, "Enter")
        elif dialog == "onboarding":
            # Claude Code's first run on a machine: the theme picker, then
            # the security notes. Enter takes the marked default on both;
            # the poll comes back for the next screen. Left alone, the
            # first session on a fresh machine sits at the theme picker
            # and everything typed into it is swallowed.
            self._tmux("send-keys", "-t", sess.name, "Enter")
        elif dialog == "auth" and not sess.state.get("auth_reported"):
            sess.state["auth_reported"] = True
            application_log("runtime", "runtime.cli_needs_auth",
                            f"{self.adapter.display} is asking the user to "
                            "sign in; nothing can be typed for them",
                            severity="warning", task_id=sess.task_id)

    async def _discover_session_file(self, sess: _TmuxSession,
                                     existing: set) -> None:
        project_dir = self.adapter.transcript_dir(sess.working_directory)
        deadline = time.monotonic() + self.startup_timeout
        if project_dir is None:
            # A CLI that writes no transcript has no id of its own to
            # discover. It is a session once its prompt is up; we name
            # it, and the screen is what gets watched.
            await self._wait_resumed(sess)
            if not await self._off_loop(self._alive, sess.name):
                sess.status = "failed"
                sess.ready.set()
                return
            sess.session_id = "scr_" + secrets.token_hex(6)
            self.sessions[sess.session_id] = sess
            sess.ready.set()
            return
        while time.monotonic() < deadline:
            await self._off_loop(self._handle_startup_prompts, sess)
            fresh = [p for p in self.adapter.transcripts(sess.working_directory)
                     if str(p) not in existing]
            if fresh:
                newest = max(fresh, key=lambda p: p.stat().st_mtime)
                sess.jsonl_path = newest
                sess.session_id = self.adapter.session_id_of(newest)
                self.sessions[sess.session_id] = sess
                sess.ready.set()
                return
            if not await self._off_loop(self._alive, sess.name):
                break
            await asyncio.sleep(0.2)
        sess.status = "failed"
        sess.ready.set()

    # -- CodingAgentRuntime -------------------------------------------------
    async def create_session(self, task_id: str, working_directory: str,
                             initial_prompt: str) -> str:
        for sess in self.sessions.values():
            if sess.task_id == task_id and sess.status != "disconnected":
                raise RuntimeError(f"task {task_id} already has a live "
                                   f"execution ({sess.session_id})")
        return await self.launch_session(
            task_id, working_directory,
            self.adapter.launch_argv(initial_prompt, self.permission_mode))

    async def launch_session(self, task_id: str, working_directory: str,
                             argv: list[str],
                             existing: set | None = None,
                             session_id: str | None = None,
                             focus: bool = True) -> str:
        """Start a claude process with this exact command line and
        supervise it.

        focus: whether the new session's window is put in front of the
        user as it starts. A worker is; the Boss is not, until the
        conversation has gone past small talk (bring_forward). A tmux
        pane has no window of ours to raise either way.

        The seam under create_session, split out because not every
        session is a worker with a prompt: the Boss is a claude session
        too - with its own tools, its own instructions directory, and a
        session id to resume - and it is hosted, watched and typed into
        exactly the way a worker is.

        existing: transcript files to ignore when discovering this
        session's own. Defaults to everything already in the project dir,
        which is right for a new session; a caller resuming an old one
        passes the set without that session's file.

        session_id: the id this session WILL have, when the caller pinned
        it (--session-id) or is resuming it (--resume). Then there is
        nothing to discover: a session started without a first message
        writes no transcript until it is spoken to, so waiting for one is
        waiting for ever - measured: the Boss sat at its prompt, wrote
        nothing, and was killed as "did not start". Known-id sessions are
        adopted at their prompt instead.
        """
        name = session_name(task_id)
        # A fresh task's name should be free. That it is not means a
        # previous run left something behind under it - said out loud,
        # because a name silently taken over is how one worker ends up
        # answering for another. A name with a LIVE worker under it is
        # not something to close: this call would be the second process
        # on one conversation, so it does not happen.
        claim = await self._claim_name(name, working_directory, task_id)
        if claim == "live":
            raise DuplicateSession(
                f"{name} is already hosting a running worker; starting "
                "another would put two processes on one conversation")
        project_dir = self.adapter.transcript_dir(working_directory)
        if existing is None:
            existing = {str(p) for p in
                        self.adapter.transcripts(working_directory)}
        result = await self._off_loop(self._tmux, "new-session", "-d", "-s", name,
                            "-c", working_directory,
                            *scrub_argv(task_id, self.home), *argv)
        # Claude Code asks tmux for the alternate screen, and the
        # alternate screen keeps no history: the Boss window's terminal
        # view had nothing to scroll (measured 2026-09-01: alternate_on=1,
        # history_size=0, both captures 24 lines). With the option off
        # the transcript accumulates in the normal buffer, the way it
        # does in a plain terminal.
        await self._off_loop(self._tmux, "set-option", "-w", "-t", name,
                             "alternate-screen", "off")
        # And the raw byte stream, from birth - see stream_path.
        stream = stream_path(name)
        stream.parent.mkdir(parents=True, exist_ok=True)
        await self._off_loop(self._tmux, "pipe-pane", "-t", name, "-o",
                             f"cat >> '{stream}'")
        unconfirmed = result.returncode != 0
        if unconfirmed:
            if not await self._off_loop(self._alive, name):
                raise RuntimeError(f"tmux failed: {result.stderr.strip()}")
            # The host made the session but could not confirm the command
            # was taken - under cmux, the typed line was never seen whole
            # on a screen cmux may not have been drawing. Measured
            # 2026-08-29, three launches in a row: claude was running in
            # every one; the task was marked failed, its worktree deleted
            # under it, and the Boss started a fourth worker on the same
            # job. What a session wrote is the evidence: adopt it if it
            # started, close it if it did not, and never leave it behind.
            application_log("runtime", "launch.unconfirmed",
                            f"{name}: {result.stderr.strip()}; judging by "
                            f"the session's transcript", severity="warning",
                            task_id=task_id)
        if session_id is None:
            return await self.adopt_session(task_id, name, working_directory,
                                            existing)
        sid = await self._adopt_known(task_id, name, working_directory,
                                      session_id, project_dir)
        if unconfirmed and not self.adapter.prompt_ready(await self._pane(name)):
            sess = self.sessions.pop(sid, None)
            if sess is not None and sess.watcher:
                sess.watcher.cancel()
            await self._off_loop(self._tmux, "kill-session", "-t", name)
            raise RuntimeError(f"tmux failed: {result.stderr.strip()}, and "
                               f"no claude prompt appeared")
        return sid

    def bring_forward(self, session_id: str) -> None:
        """Put a running session's window in front of the user. Nothing
        to do for a tmux pane: the Terminal window is the user's own."""

    async def _adopt_known(self, task_id: str, name: str,
                           working_directory: str, session_id: str,
                           project_dir: Path) -> str:
        """Supervise a session whose id is already known: wait for its
        prompt, watch the transcript it will write, and read on from the
        end of one it already wrote."""
        path = project_dir / f"{session_id}.jsonl" \
            if project_dir is not None else None
        sess = _TmuxSession(task_id=task_id, name=name,
                            working_directory=working_directory,
                            session_id=session_id, jsonl_path=path,
                            offset=path.stat().st_size
                            if path is not None and path.exists() else 0)
        self.sessions[session_id] = sess
        await self._wait_resumed(sess)
        if not self._alive(name):
            self.sessions.pop(session_id, None)
            raise RuntimeError("claude session exited before reaching its "
                               "prompt")
        sess.status = "idle"
        if self.transcript is not None:
            self.transcript.header(task_id, f"task {task_id} (interactive)",
                                   session_id, working_directory)
        sess.watcher = asyncio.create_task(self._watch(sess))
        self._emit(sess, AgentEvent(type="started"))
        self.bus.emit(ObservabilityEvent(
            type="runtime.session_created", component="runtime",
            task_id=task_id, provider_session_id=session_id,
            data={"pty": name, "interactive": True, "known_id": True,
                  # Where this process's count of the session's events
                  # starts, and in which epoch: the line that would have
                  # made "61 events dropped as stale after a restart" a
                  # one-line grep.
                  "epoch": getattr(self, "epoch", ""),
                  "sequence_from": sess.sequence,
                  "transcript_offset": sess.offset}))
        return session_id

    async def adopt_session(self, task_id: str, name: str,
                            working_directory: str,
                            existing: set | None = None) -> str:
        """Supervise a claude already running in the tmux session `name`.

        Split out of create_session because not every worker is started by
        this runtime: a cloud session teleported down to a checkout is a
        real local claude in a real pane, and everything after the launch -
        finding its transcript, watching it, reporting its turns - is
        identical. Duplicating that for the cloud runtime would mean two
        copies of the part most likely to drift.
        """
        if existing is None:
            # No pre-launch snapshot: any transcript in this project dir
            # could be ours, so let the discovery pick the newest.
            existing = set()
        sess = _TmuxSession(task_id=task_id, name=name,
                            working_directory=working_directory)
        await self._discover_session_file(sess, existing)
        if sess.session_id is None:
            await self._off_loop(self._tmux, "kill-session", "-t", name)
            raise RuntimeError("claude session did not start (no session "
                               "file appeared)")
        if self.transcript is not None:
            self.transcript.header(task_id, f"task {task_id} (interactive)",
                                   sess.session_id, working_directory)
        sess.watcher = asyncio.create_task(self._watch(sess))
        self._emit(sess, AgentEvent(type="started"))
        self.bus.emit(ObservabilityEvent(
            type="runtime.session_created", component="runtime",
            task_id=task_id, provider_session_id=sess.session_id,
            data={"pty": name, "interactive": True}))
        return sess.session_id

    async def executions(self) -> list[TaskExecution]:
        return [TaskExecution(
                    task_id=sess.task_id, provider=self.adapter.name,
                    provider_session_id=sess.session_id or "",
                    workspace_path=sess.working_directory,
                    status=sess.status,
                    transcript_path=str(self.transcript.path(sess.task_id))
                    if self.transcript else None,
                    pty_handle=sess.name)
                for sess in self.sessions.values()
                if sess.status != "disconnected"]

    def attach_handle(self, task_id: str) -> str | None:
        for sess in self.sessions.values():
            if sess.task_id == task_id and sess.status != "disconnected":
                return sess.name
        return None

    async def send(self, session_id: str, message: str) -> None:
        """Type into the session, once it is actually listening.

        send-keys puts characters wherever the TUI's focus happens to be. Mid
        turn the Enter is swallowed and the text sits unsent in the input box;
        with a permission dialog up the keystrokes answer the dialog instead,
        which loses the message and can resolve an approval nobody decided.
        So: wait for the input prompt, type, then confirm the box actually
        cleared - a message that did not go in is an error, not a silence.
        """
        sess = self._require(session_id)
        if not await self._off_loop(self._alive, sess.name):
            raise RuntimeError(f"cannot send to {session_id}: PTY is gone")
        draft = await self._wait_for_input(sess)
        if draft:
            # Set the user's draft aside. Ctrl-U clears the box and nothing
            # else - measured against a live claude: the draft goes, the
            # session stays at its prompt, nothing is submitted, and the
            # same text types straight back in.
            await self._off_loop(self._tmux, "send-keys", "-t", sess.name,
                                 "C-u")
        for argv in self._send_argv(sess.name, message):
            result = await self._off_loop(self._tmux, *argv)
            if result.returncode != 0:
                raise RuntimeError(f"send-keys failed: {result.stderr}")
        await self._confirm_submitted(sess, message)
        if draft:
            # Ours is in; theirs goes back where it was, unsent. One line
            # of it: that is what the screen showed and what was read.
            await self._off_loop(self._tmux, "send-keys", "-t", sess.name,
                                 "-l", draft)
            application_log("runtime", "send.draft_restored",
                            f"{sess.name}: the user's draft is back in the box",
                            severity="debug", draft=draft[:80])
        sess.status = "running"

    # What Claude Code shows while it is generating. Text in the box under
    # this is a queued message, not an idle draft, and Enter would submit
    # both into the queue.
    BUSY_MARK = "esc to interrupt"

    async def _wait_for_input(self, sess: "_TmuxSession",
                              timeout: float = 45.0) -> str:
        """Block until the session can take a message. Returns whatever the
        user had half-typed in the box - "" when it was empty.

        A draft used to mean waiting. send-keys appends to the box and
        presses Enter, so typing over a draft submits theirs welded to ours;
        so this waited for the box to clear, 45 seconds, and then failed.
        Measured on the Boss window: two stray characters ('s now') and
        every spoken message for the next minute died with "someone is
        typing there; message not sent". The person still wins - nothing
        of theirs is ever submitted - but the message goes: the caller sets
        the draft aside, sends, and puts it back (see send).
        """
        deadline = time.monotonic() + timeout
        probed = False
        while time.monotonic() < deadline:
            # A boot dialog is not the user's approval to answer; clear it.
            await self._off_loop(self._handle_startup_prompts, sess)
            pane = await self._pane(sess.name)
            if self.adapter.approval_prompt(pane) is not None:
                await asyncio.sleep(0.4)      # a dialog owns the keyboard
                continue
            typed = _typed_but_unsent(pane)
            if typed and not probed:
                # Text in the box is not proof that anyone typed it: a
                # recap leaves Claude Code's own suggested next prompt
                # drawn there, and it reads exactly like typing. Ask once.
                probed = True
                if await self._ghost_in_the_box(sess):
                    typed = ""      # a suggestion: our text replaces it
            if not typed and self.adapter.prompt_ready(pane):
                return ""
            if typed and self.BUSY_MARK not in pane.lower():
                # A person's draft, and the session is idle. It is handed
                # back to be set aside and restored, not waited on. (The
                # shortcuts hint that PROMPT_READY looks for disappears
                # once anything is typed, so idleness is read as the
                # absence of work instead.)
                return typed
            await asyncio.sleep(0.3)
        raise RuntimeError(
            f"{sess.name} never returned to its prompt; message not sent")

    async def _ghost_in_the_box(self, sess: "_TmuxSession") -> bool:
        """Is the text in the input box Claude Code's own suggestion?

        After a recap, Claude Code draws a suggested next prompt in the
        empty box. On screen it is indistinguishable from typing -
        measured on the live Boss: both are the prompt character, U+00A0,
        then text - and cmux exposes no styling. From 23:30 to 23:52 that
        suggestion held back every Manager turn as "someone is typing
        there", and the user heard that their message did not go out.

        Behaviour tells them apart where the pixels cannot: typing
        REPLACES a suggestion and APPENDS to typed text (measured: one
        character in showed only that character; deleting it brought the
        suggestion back). So: one character in, one look, one character
        out. A person mid-sentence sees a stray character for a third of
        a second and keeps their words; the box is left exactly as found.
        """
        name = sess.name
        await self._off_loop(self._tmux, "send-keys", "-t", name, "-l", PROBE)
        await asyncio.sleep(0.3)
        after = _typed_but_unsent(await self._pane(name))
        await self._off_loop(self._tmux, "send-keys", "-t", name, "BSpace")
        return after == PROBE

    async def _confirm_submitted(self, sess: "_TmuxSession", message: str,
                                 timeout: float = 4.0) -> None:
        """The text leaving the input box is the acknowledgement."""
        tail = " ".join(message.split())[-40:]
        if not tail:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pane = await self._pane(sess.name)
            lines = [l.strip() for l in pane.splitlines() if l.strip()]
            pending = next((l for l in reversed(lines)
                            if l.startswith(("\u276f", ">"))), "")
            if tail not in " ".join(pending.split()):
                return                        # submitted
            await asyncio.sleep(0.3)
        raise RuntimeError(
            f"message stayed in {sess.name}'s input box; not submitted")

    async def interrupt(self, session_id: str) -> None:
        sess = self._require(session_id)
        await self._off_loop(self._tmux, "send-keys", "-t", sess.name, "Escape")
        sess.status = "idle"

    async def _claim_name(self, name: str, cwd: str,
                          task_id: str) -> str:
        """Make sure `name` is free before a new-session uses it.

        Session names are not decoration: they are how the surface, the
        sweep and every follow-up find a worker, and two things answering
        to one name is two things on one conversation. tmux says so - it
        refuses the second new-session with `duplicate session: NAME` -
        but only the caller that reads its stderr ever hears it, and
        resume() turned that into one more "could not resume" string in
        one more error message.

        2026-09-08, in full: every message to task_b152b744 bounced with
        `error: could not resume: duplicate session: cond_task_b152b744`.
        `tmux ls` afterwards showed why - that session had been created
        on Sep 3 and was still listed a week later, an empty window a
        restart had left behind with no claude in it. _live_worker_in was
        right to refuse to adopt it, and then resume created a session
        under a name tmux already had. Nobody closed the empty window,
        nobody said the name was the problem, and the log carried no line
        about it at all.

        So the collision is settled here, out loud, before it happens:

            free       nobody has the name
            live       a worker is running under it - do NOT create a
                       second one; adopt what is there
            reclaimed  a window with no process in it, closed so the name
                       can be used
        """
        if not await self._off_loop(self._alive, name):
            return "free"
        probe = _TmuxSession(task_id=task_id, name=name,
                             working_directory=cwd)
        if await self._off_loop(self._process_alive, probe):
            application_log(
                "runtime", "runtime.session_name_taken",
                f"{name} is already hosting a running worker; not "
                "starting a second one on the same conversation",
                severity="error", task_id=task_id,
                data={"session": name, "working_directory": cwd})
            return "live"
        application_log(
            "runtime", "runtime.session_name_reclaimed",
            f"{name} was still listed but nothing is running in it; "
            "closing it so the name can be used again",
            severity="warning", task_id=task_id,
            data={"session": name, "working_directory": cwd})
        await self._off_loop(self._tmux, "kill-session", "-t", name)
        return "reclaimed"

    def _live_worker_in(self, cwd: str) -> str | None:
        """The session name of a worker already running in this checkout.

        One checkout per task, named for the task, so the checkout names
        the session: `.../workspaces/proj_x/task_abc` -> `cond_task_abc`.
        """
        leaf = os.path.basename(os.path.normpath(cwd))
        if not leaf.startswith("task_"):
            return None
        name = session_name(leaf)
        if not self._alive(name):
            return None
        # A window is not a worker. Adopting a cmux workspace whose claude
        # had exited would have supervised an empty shell - and never
        # started the process the resume was for.
        probe = _TmuxSession(task_id=leaf, name=name, working_directory=cwd)
        return name if self._process_alive(probe) else None

    def _readopt(self, session_id: str, cwd: str, name: str,
                 recover_finish: bool = False) -> None:
        """Supervise a worker that is already running, instead of starting
        another one on its conversation.

        Watching picks up from the END of the transcript: the turns it
        already finished are history, and replaying them would announce
        old work as if it had just happened. But the LAST turn, when the
        caller knows nothing was delivered (recover_finish - the store
        still calls the task running), is not history: a finish inside
        the unwatched window produced no event, the Boss was never told,
        and the task said "running" for ever over a worker sitting on its
        answer. Its finish is noted here and delivered by
        deliver_adopted_finish once someone has subscribed - emitting it
        now would reach no one.
        """
        sess = _TmuxSession(task_id=os.path.basename(os.path.normpath(cwd)),
                            name=name, working_directory=cwd,
                            session_id=session_id, status="idle")
        path = self.adapter.transcript_for(cwd, session_id)
        if path is not None and path.exists():
            sess.jsonl_path = path
            sess.offset = path.stat().st_size
            if recover_finish:
                summary = self.adapter.finished_turn(path)
                if summary is not None:
                    sess.state["adopted_finish"] = summary
        sess.ready.set()
        self.sessions[session_id] = sess
        sess.watcher = asyncio.create_task(self._watch(sess))

    async def resume(self, session_id: str,
                     working_directory: str | None = None) -> None:
        sess = self.sessions.get(session_id)
        if sess is not None and await self._off_loop(self._alive, sess.name):
            return                    # the PTY survived; nothing to do
        cwd = working_directory or (sess.working_directory if sess else None)
        if cwd is None:
            raise RuntimeError("resume needs a working_directory")
        # Our own memory is not evidence that nothing is running. A
        # Conductor restart empties self.sessions while the worker it
        # started keeps going, so this guard used to pass and resume would
        # launch a SECOND process on the same conversation. Measured live
        # after a day of restarts: pid 43223 (the worker) and pid 47921
        # (claude --resume of its session) both on b1ca8fe7 for nearly
        # three hours, in two workspaces, on one task.
        if sess is None:
            running = await self._off_loop(self._live_worker_in, cwd)
            if running is not None:
                self._readopt(session_id, cwd, running)
                return
        if sess is not None:
            task_id, name = sess.task_id, sess.name
        else:
            # The checkout names the session, so whatever comes looking
            # later can find this worker. Inventing cond_resumed_<session>
            # instead meant the SURFACE looked for cond_task_<id>, missed,
            # and resumed the same conversation a second time - which
            # Claude Code reports in the new tab as "another Claude Code
            # on this machine already has Remote Control for this
            # conversation", and which is two processes on one
            # conversation however politely it is phrased.
            leaf = os.path.basename(os.path.normpath(cwd))
            task_id = leaf if leaf.startswith("task_") \
                else f"resumed_{session_id[:8]}"
            name = session_name(task_id)
        # The name may be taken even when nothing above found a worker to
        # adopt: _live_worker_in only looks at checkouts named for a task,
        # and it did not run at all when we remember a session whose PTY
        # we think is gone. Then new-session met a name tmux already had
        # and answered `duplicate session: cond_task_b152b744` - which
        # reached the user as "could not resume", twice, with no hint
        # that the name was the whole problem. Settle it before creating.
        claim = await self._claim_name(name, cwd, task_id)
        if claim == "live":
            # Something is already running under this name. Supervise
            # that, rather than starting a rival on the same transcript -
            # and drop the watcher on the session we thought was gone,
            # or the transcript would have two readers.
            stale = self.sessions.pop(session_id, None)
            if stale is not None and stale.watcher is not None:
                stale.watcher.cancel()
            self._readopt(session_id, cwd, name)
            return
        result = await self._off_loop(
            self._tmux, "new-session", "-d", "-s", name, "-c", cwd,
            *scrub_argv(task_id, self.home),
            *self.adapter.resume_argv(session_id))
        if result.returncode != 0:
            detail = result.stderr.strip()
            if "duplicate session" in detail.lower():
                application_log(
                    "runtime", "runtime.session_name_collision",
                    f"tmux already has a session called {name}; the "
                    "resume was refused rather than run beside it",
                    severity="error", task_id=task_id,
                    provider_session_id=session_id,
                    data={"session": name, "stderr": detail[:300]})
                raise DuplicateSession(
                    f"{name} is already taken by another session - most "
                    "likely a second conductor is running. Quit it, or "
                    "start with --takeover.")
            raise RuntimeError(f"could not resume: {detail}")
        # Same as create_session: no alternate screen, so the pane keeps
        # history and the window's terminal view can scroll.
        await self._off_loop(self._tmux, "set-option", "-w", "-t", name,
                             "alternate-screen", "off")
        stream = stream_path(name)
        stream.parent.mkdir(parents=True, exist_ok=True)
        await self._off_loop(self._tmux, "pipe-pane", "-t", name, "-o",
                             f"cat >> '{stream}'")
        if sess is None:
            sess = _TmuxSession(task_id=task_id, name=name,
                                working_directory=cwd,
                                session_id=session_id)
            self.sessions[session_id] = sess
        # claude --resume takes seconds to draw its TUI. Returning before it
        # is ready means the next send-keys types into a booting screen: the
        # text lands in the input box and the Enter is swallowed, so the
        # instruction sits there unsent.
        await self._wait_resumed(sess)
        sess.status = "idle"
        if sess.watcher is None or sess.watcher.done():
            sess.watcher = asyncio.create_task(self._watch(sess))

    async def _wait_resumed(self, sess: "_TmuxSession") -> None:
        """Wait for a resumed session to present its input prompt."""
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            await self._off_loop(self._handle_startup_prompts, sess)
            pane = await self._pane(sess.name)
            if self.adapter.prompt_ready(pane):
                sess.ready.set()
                return
            if not await self._off_loop(self._alive, sess.name):
                break
            await asyncio.sleep(0.3)
        sess.ready.set()   # do not block forever; send will surface the error

    # How long one provider listing (`claude agents --json`, ~0.4s) is
    # believed. After a restart every worker is unknown to this runtime,
    # and "what is everything doing?" asked about each of nineteen in
    # turn - measured: list_subagents took 14.5s for the Boss, all of it
    # this listing, nineteen times. Within a second they all say the same.
    FEED_TTL_S = 1.0

    async def _feed(self) -> list[dict]:
        """The provider's session listing, shared by everyone who asks
        within FEED_TTL_S. One fetch at a time: concurrent askers wait
        for the fetch in flight rather than each starting their own."""
        cached = getattr(self, "_feed_cache", None)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self.FEED_TTL_S:
            return cached[1]
        lock = getattr(self, "_feed_lock", None)
        if lock is None:
            lock = self._feed_lock = asyncio.Lock()
        async with lock:
            cached = getattr(self, "_feed_cache", None)
            if cached is not None and time.monotonic() - cached[0] < self.FEED_TTL_S:
                return cached[1]
            rows = await agent_feed.sessions(claude=self.claude)
            self._feed_cache = (time.monotonic(), rows)
            return rows

    async def get_status(self, session_id: str) -> str:
        sess = self.sessions.get(session_id)
        if sess is None:
            # This runtime has no record - but that is what a restart looks
            # like, not what a dead worker looks like. The panes outlive the
            # app, so after every restart every existing worker was being
            # reported disconnected while it sat there alive. Ask the
            # provider before declaring one gone.
            row = agent_feed.find(await self._feed(), session_id=session_id)
            return agent_feed.status_of(row) if row else "disconnected"
        if not await self._off_loop(self._alive, sess.name):
            return "disconnected"
        # Not consulted when we DO have a record: measured against real
        # workers - a quick task, a long one, and a command auto mode stops
        # to ask about - the feed and the transcript agreed at every
        # sample, so asking would be a subprocess per status check to learn
        # nothing.
        return sess.status

    def live_session_names(self) -> set | None:
        """Every PTY tmux currently has, by name - or None if we could not
        ask.

        The difference matters more than it looks. This answers "which
        workers are still alive", and the sweep marks everything missing
        from it as gone. A failed listing used to come back as an empty
        set, which reads as "they all died": two healthy workers were
        declared gone within a minute of starting, their cards went stale,
        and their workspaces were sitting in cmux the whole time. Not
        knowing is not the same as knowing there are none.
        """
        result = self._tmux("list-sessions", "-F", "#{session_name}")
        if result.returncode != 0:
            # tmux says "no server running" when there are genuinely no
            # sessions, which IS an answer; anything else is a failure to
            # ask, and cmux failures all land here too.
            text = (result.stderr or "").lower()
            if "no server running" not in text and "no sessions" not in text:
                return None
        return {line.strip() for line in result.stdout.splitlines()
                if line.strip()}

    def ensure_watched(self, session_id: str,
                       working_directory: str) -> bool:
        """Watch a worker that is running but that nobody is reading.

        Cards are built from what the watcher sees in the transcript, and
        the transcript records every turn regardless of who caused it - so
        a message the user TYPES into the worker's own window produces a
        completion exactly like a spoken one does. What it does not
        produce is a card, because the watcher is a task per session and
        only exists for sessions this process started. After a restart,
        or for a worker adopted from a previous run, nobody is reading:
        the user types, the agent answers, and the card sits there stale.

        Cheap and idempotent: it does nothing when a live watcher already
        exists, which is the common path.
        """
        sess = self.sessions.get(session_id)
        if sess is not None and sess.watcher is not None \
                and not sess.watcher.done():
            return False
        name = self._live_worker_in(working_directory)
        if name is None:
            return False                  # not running; nothing to watch
        if sess is None:
            self._readopt(session_id, working_directory, name,
                          recover_finish=True)
            return True
        sess.watcher = asyncio.create_task(self._watch(sess))
        return True

    async def ensure_watched_async(self, session_id: str,
                                   working_directory: str) -> bool:
        """ensure_watched for the sweep: the one blocking probe off the
        loop, the watcher created on it.

        The sweep moved ensure_watched into a thread because its probe is
        a subprocess (#43, #44). But the watcher it starts is an asyncio
        task, and asyncio.create_task in a thread raises "no running event
        loop" - so from then on the sweep failed every 40 s
        (task.watch_failed) and no adopted worker was watched at all: the
        user typed into a worker, it answered, the card never moved.
        """
        sess = self.sessions.get(session_id)
        if sess is not None and sess.watcher is not None \
                and not sess.watcher.done():
            return False
        name = await self._off_loop(self._live_worker_in, working_directory)
        if name is None:
            return False
        if sess is None:
            self._readopt(session_id, working_directory, name,
                          recover_finish=True)
            return True
        return self.rewatch(session_id)

    def deliver_adopted_finish(self, session_id: str) -> bool:
        """Emit the finish an adopted worker reached while nobody read it.

        Kept separate from _readopt because ordering is the point:
        adoption creates the session, subscribers attach to the session,
        and only then can the recovered finish reach them.
        """
        sess = self.sessions.get(session_id)
        if sess is None:
            return False
        summary = sess.state.pop("adopted_finish", None)
        if summary is None:
            return False
        sess.status = "idle"
        self._emit(sess, AgentEvent(type="completed", summary=summary,
                                    detail={"recovered": True}))
        application_log("runtime", "runtime.finish_recovered",
                        f"{sess.name} had finished before anyone was "
                        "reading it; its answer is delivered now",
                        task_id=sess.task_id,
                        provider_session_id=session_id)
        return True

    def rewatch(self, session_id: str) -> bool:
        """Start reading a known session again after its watcher exited.

        A watcher is a task per session, and a task can end: it decides
        the session is gone (rightly or, until this run, on one bad cmux
        listing), or the loop it lived on is torn down. Nothing brought
        one back. Measured 2026-08-29, 15:16:58Z: the Boss's watcher
        ended, the Boss went on answering in its transcript, and for the
        next six hours every spoken turn waited out the Boss timeout and
        got "still in progress" - nobody was reading. Restarting picks up
        from where the last watcher stopped, so a reply written meanwhile
        is delivered rather than lost.

        Cheap and idempotent: False when the session is unknown or a live
        watcher already exists. Must be called on the event loop.
        """
        sess = self.sessions.get(session_id)
        if sess is None:
            return False
        if sess.watcher is not None and not sess.watcher.done():
            return False
        sess.watcher = asyncio.create_task(self._watch(sess))
        application_log("runtime", "runtime.watch_restarted",
                        f"reading {sess.name} again from offset "
                        f"{sess.offset}", task_id=sess.task_id,
                        provider_session_id=sess.session_id)
        return True

    def unwatched_prompts(self) -> list[dict]:
        """Panes sitting on a decision that nothing is watching.

        Approval prompts are noticed by _watch, which is a task per session
        and only exists for sessions THIS process started. Startup leaves
        workers asleep, so after a restart a backgrounded worker that hits
        a prompt has nobody reading its pane: it waits, silently, for a
        card that will never appear. That is the "it needed approval and
        got stuck" case, and it is invisible precisely because the part
        that would have seen it is gone.

        Reported, never answered. A prompt that reached here was not
        cleared by policy, so it is one of the consequential ones - the
        user's call, not ours.
        """
        found = []
        watched = {sess.name for sess in self.sessions.values()
                   if sess.watcher is not None and not sess.watcher.done()}
        for name in (self.live_session_names() or set()):
            if name in watched or not name.startswith("cond_"):
                continue
            pane = self._tmux("capture-pane", "-t", name, "-p").stdout
            question = _prompt_in(pane)
            if question:
                found.append({"session": name, "question": question})
        return found

    async def unstick(self) -> list[dict]:
        """Clear anything holding a worker at a screen it cannot leave.

        Boot dialogs are the common one: nobody is watching a delegated
        worker's window, so a resume picker or a trust prompt waits for a
        keypress that is never coming, and every message to that worker
        times out. Run periodically - a session can hit one of these long
        after startup, when it is woken.
        """
        fixed = []
        for name in (await self._off_loop(self.live_session_names) or set()):
            pane = await self._pane(name)
            low = pane.lower()
            if "resume from summary" in low or "resume full session" in low:
                await self._off_loop(self._tmux, "send-keys", "-t", name, "Enter")
                fixed.append({"session": name, "cleared": "resume picker"})
            elif "trust this folder" in low:
                await self._off_loop(self._tmux, "send-keys", "-t", name, "Enter")
                fixed.append({"session": name, "cleared": "trust dialog"})
        return fixed

    async def reconcile_session(self, session_id: str) -> str:
        sess = self.sessions.get(session_id)
        if sess is not None and sess.pending_approval is not None:
            return "waiting_for_approval"
        status = await self.get_status(session_id)
        return {"running": "running", "idle": "idle",
                "waiting_for_approval": "waiting_for_approval",
                "starting": "starting"}.get(status, "unreachable")

    async def pending_approvals(self, session_id: str) -> list[dict]:
        sess = self.sessions.get(session_id)
        if sess is None or sess.pending_approval is None:
            return []
        return [dict(sess.pending_approval)]

    async def resolve_approval(self, session_id: str, approval_id: str,
                               approve: bool) -> None:
        """Answer the prompt through the exact PTY that owns it, then verify
        the provider advanced: the prompt disappearing from THIS pane is the
        acknowledgement. Sent-but-still-waiting is a delivery failure."""
        sess = self._require(session_id)
        approval = sess.pending_approval
        if approval is None or approval["approval_id"] != approval_id:
            raise KeyError(f"approval {approval_id} is unknown or already "
                           "resolved")
        pane = await self._pane(sess.name)
        if self.adapter.approval_prompt(pane) is None:
            # Stale: the prompt is gone (user answered, or it invalidated).
            sess.pending_approval = None
            self._observe_approval(sess, "approval.stale",
                                   approval_id=approval_id)
            raise KeyError(f"approval {approval_id} is no longer pending")
        self._observe_approval(sess, "approval.decision_sending",
                               approval_id=approval_id,
                               decision="approved" if approve else "denied")
        sess.state["deciding"] = True   # ours to resolve; watcher stands by
        # The keys are the CLI's: the adapter reads them off the prompt.
        keys = self.adapter.approve_keys(pane) if approve \
            else self.adapter.deny_keys(pane)
        for sequence in keys:
            await self._off_loop(self._tmux, "send-keys", "-t", sess.name,
                                 *sequence)
        self._observe_approval(sess, "approval.decision_sent",
                               approval_id=approval_id)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                await asyncio.sleep(0.3)
                pane = await self._pane(sess.name)
                if self.adapter.approval_prompt(pane) is None:
                    sess.pending_approval = None
                    sess.status = "running"
                    self._emit(sess, AgentEvent(
                        type="approval_resolved",
                        detail={"approval_id": approval_id,
                                "decision": "approved" if approve
                                else "denied",
                                "resolved_by": "conductor"}))
                    self._observe_approval(sess, "approval.resolved",
                                           approval_id=approval_id,
                                           resolved_by="conductor")
                    return
            self._observe_approval(sess, "approval.delivery_failed",
                                   approval_id=approval_id)
            raise RuntimeError(f"approval {approval_id} decision was sent "
                               "but the prompt did not clear (delivery "
                               "failure)")
        finally:
            sess.state.pop("deciding", None)

    async def subscribe(self, session_id: str, handler: EventHandler):
        sess = self._require(session_id)
        sess.handlers.append(handler)

        def unsubscribe() -> None:
            try:
                sess.handlers.remove(handler)
            except ValueError:
                pass
        return unsubscribe

    async def destroy(self, session_id: str) -> None:
        sess = self.sessions.pop(session_id, None)
        if sess is None:
            return
        if sess.watcher:
            sess.watcher.cancel()
        # The last look: the pane dies here, so save what it showed. A
        # finished worker's card kept opening onto "The session has
        # ended." over nothing (measured 2026-09-01); now the Boss
        # window's terminal view can show the screen it ended with.
        try:
            done = await self._off_loop(
                self._tmux, "capture-pane", "-e", "-p", "-S", "-2000",
                "-t", sess.name)
            if done.returncode == 0 and done.stdout.strip():
                path = final_screen_path(sess.name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(done.stdout)
        except Exception:
            pass                 # a missing memento must not block the end
        await self._off_loop(self._tmux, "kill-session", "-t", sess.name)
        sess.status = "disconnected"

    def _require(self, session_id: str) -> _TmuxSession:
        sess = self.sessions.get(session_id)
        if sess is None:
            raise KeyError(f"no live session {session_id}")
        return sess
