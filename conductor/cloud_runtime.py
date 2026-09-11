"""CloudClaudeRuntime: workers that live in Claude Code cloud sessions.

A cloud session runs on Anthropic's infrastructure, shows up at
claude.ai/code (so it is visible from a phone), and costs the user no extra
account - it uses the Claude login they already have. What it cannot do is
talk back: the CLI says so itself, "a cloud session cannot message other
sessions back yet". Sending returns a receipt, never a reply.

So this runtime is deliberately two-phase.

  CLOUD    started, steerable, and BLIND. We can create it and send it
           follow-ups; we cannot read its transcript, its status, or its
           approval prompts. Nothing is inferred from that silence: an
           unreadable worker reports "running" and says why, rather than
           being guessed at.

  TELEPORTED  `claude --teleport <id>` in a checkout resumes the whole
           session locally, in a real pane, with its full history. It
           becomes a session you can watch and type into, and its status
           becomes observable through `claude agents --json`.

           It does NOT become a fully supervised worker. This was built on
           the assumption that it would - that the tmux runtime could adopt
           it like any other pane - and testing against a real session
           proved that wrong: a teleported session writes no local
           transcript at all, nothing under ~/.claude, no open file
           handles. Our completion, approval and question events all come
           from that transcript, so they do not exist for a teleported
           worker. Status does, and that is what is claimed here.

Teleport is the piece that makes this honest. It is the attach primitive
the Claude desktop app does not have, and without it a cloud worker would
be fire-and-forget.

Two constraints are the provider's, not ours: creating a cloud session
requires a TTY (a tmux pane is one), and a cloud session refuses
bypassPermissions - so a cloud worker WILL stop and ask, and nobody is
watching it until it is teleported.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable

from . import agent_feed
from .agent_events import AgentEvent
from .runtime import CodingAgentRuntime, EventHandler

# "Created cloud session: ..." then a View: line carrying the id.
SESSION_ID = re.compile(r"\b(session_[A-Za-z0-9]+|cse_[A-Za-z0-9]+)\b")

CREATE_TIMEOUT_S = 90.0


@dataclass
class _CloudWorker:
    task_id: str
    session_id: str
    working_directory: str
    url: str = ""
    finished: bool = False
    teleported: bool = False
    pane: str = ""
    last_seen: str = ""
    last_notified: str = ""
    last_asked: str = ""
    watcher: object = None
    last_status: str = ""
    handlers: list = field(default_factory=list)


class CloudClaudeRuntime(CodingAgentRuntime):
    def __init__(self, local=None, claude_binary: str | None = None,
                 create_timeout: float = CREATE_TIMEOUT_S,
                 reveal: bool = True, prefer_app: bool = False,
                 poll_seconds: float = 3.0,
                 cloud_poll_seconds: float = 90.0) -> None:
        self.poll_seconds = poll_seconds
        # How often to go and look at a worker still in the cloud. A peek
        # costs a process and several seconds, so this is minutes-scale on
        # purpose: it is the difference between finding out on your own and
        # having to ask, not a live feed. Set 0 to never look.
        self.cloud_poll_seconds = cloud_poll_seconds
        # A cloud worker has no window here, so a worker that starts and
        # says nothing is work happening where the user cannot see it.
        # Starting one opens its page.
        #
        # What made this a problem the first time was not opening it once -
        # it was opening it AGAIN: probing this repeatedly left 38 tabs. So
        # a session is surfaced at most once, and every later show() for it
        # is a no-op. One worker, one tab, however many times anything asks.
        self.reveal = reveal
        # The web, because only the web can address a session.
        #
        # Claude for Mac registers exactly three deep links - new,
        # needs-input, and continue?session=last - and none of them takes a
        # session id. claude://code/<id> is not a route: it opens the app
        # and lands wherever the app was, which is what "the exact session
        # didn't open" looks like. An earlier reading of this was wrong,
        # fooled by the wanted session happening to be the newest one.
        #
        # `open` returns 0 either way, so nothing here can detect the miss.
        # https://claude.ai/code/<id> does open that session, so that is
        # what a card leads to. Flip boss.CLOUD_PREFER_APP if the app ever
        # gains a route that takes an id.
        self.prefer_app = prefer_app
        self._shown: set[str] = set()
        # Kept for callers that pass it, and unused: handing a teleported
        # worker to the tmux runtime was the original design and it does
        # not work - that runtime waits for a transcript file a teleported
        # session never writes.
        self.local = local
        self.claude = claude_binary or shutil.which("claude") or "claude"
        self.create_timeout = create_timeout
        self.workers: dict[str, _CloudWorker] = {}

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _tmux(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", *args], capture_output=True, text=True)

    async def _claude(self, *args: str) -> str:
        """Run the CLI without a TTY. Only for the paths that allow it -
        creating a cloud session does not."""
        proc = await asyncio.create_subprocess_exec(
            self.claude, *args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_clean_env())
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError((err.decode() or out.decode()).strip()[:300])
        return out.decode()

    def _worker(self, session_id: str) -> _CloudWorker:
        worker = self.workers.get(session_id)
        if worker is None:
            raise KeyError(f"unknown cloud session {session_id}")
        return worker

    # -- lifecycle -------------------------------------------------------
    async def create_session(self, task_id: str, working_directory: str,
                             initial_prompt: str) -> str:
        for worker in self.workers.values():
            if worker.task_id == task_id:
                raise RuntimeError(
                    f"task {task_id} already has a cloud session "
                    f"({worker.session_id})")
        # --cloud refuses to run without a TTY, and says so rather than
        # silently running locally. A detached tmux pane is a real TTY.
        name = f"cloudstart_{task_id}"
        if self._tmux("has-session", "-t", name).returncode == 0:
            self._tmux("kill-session", "-t", name)
        # Two things this launch has to get right, both learned the hard
        # way. `claude --cloud` prints the session id and EXITS, and a tmux
        # session whose command exits takes the pane with it - so the id
        # was gone before it could be read. The trailing sleep holds the
        # pane open long enough to capture it.
        # And stdout must stay a TTY: --cloud refuses a pipe or a redirect
        # and runs locally instead, so the id cannot be captured to a file.
        # A stale ANTHROPIC_API_KEY takes the API path and 401s, so it is
        # dropped here as well as in the non-interactive calls.
        command = ("env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY "
                   f"{shlex.quote(self.claude)} "
                   f"--cloud {shlex.quote(initial_prompt)}; "
                   f"sleep {int(self.create_timeout) + 10}")
        launched = self._tmux(
            "new-session", "-d", "-s", name, "-c", working_directory,
            "sh", "-c", command)
        if launched.returncode != 0:
            raise RuntimeError(f"tmux failed: {launched.stderr.strip()}")
        try:
            session_id, url = await self._await_session_id(name)
        finally:
            self._tmux("kill-session", "-t", name)
        worker = _CloudWorker(task_id=task_id, session_id=session_id,
                              working_directory=working_directory, url=url)
        self.workers[session_id] = worker
        if self.cloud_poll_seconds:
            worker.watcher = asyncio.create_task(self._watch_cloud(worker))
        self._emit(worker, AgentEvent(
            type="started",
            detail={"where": "cloud", "url": url,
                    "note": "not readable until teleported"}))
        if self.reveal and url:
            self.show(session_id, force=False)
        return session_id

    def show(self, session_id: str, force: bool = True) -> str:
        """Put the session in front of the user.

        The Claude desktop app is tried first and will usually decline: it
        registers a claude:// scheme but has no route for a code session,
        and it keeps its own signed-in state, so on a machine where it has
        never been set up it lands on a sign-in wall instead. Checked, not
        assumed - so the browser is the path that actually works, and it
        is what we fall back to rather than leaving the worker invisible.
        """
        # The automatic reveal fires once per session; an explicit ask
        # always opens. Otherwise closing the tab would make the card
        # permanently dead - tapping it would report success and do
        # nothing.
        if not force and session_id in self._shown:
            return "already"
        self._shown.add(session_id)
        worker = self.workers.get(session_id)
        url = (worker.url if worker else "") or \
            f"https://claude.ai/code/{session_id}"
        if self.prefer_app and _desktop_app_handles_code_sessions():
            # Only reachable when explicitly enabled: no known route takes
            # a session id, so this lands on the app, not the session.
            opened = subprocess.run(
                ["open", f"claude://code/{session_id}"], capture_output=True)
            if opened.returncode == 0:
                return "app"          # and NOT the browser as well
        # Name the browser. A bare `open` hands https://claude.ai/... to
        # Claude for Mac, which claims those as universal links and then
        # cannot address a session - so the tab never appeared and the app
        # sat on whatever it was already showing. That is what "the exact
        # session didn't open" was: not the app's routes, the interception.
        browser = _default_browser()
        if browser:
            opened = subprocess.run(["open", "-b", browser, url],
                                    capture_output=True)
            if opened.returncode == 0:
                return "web"
        subprocess.run(["open", url], capture_output=True)
        return "web"

    async def _await_session_id(self, name: str) -> tuple[str, str]:
        """Read the id out of the pane the CLI printed it into."""
        deadline = asyncio.get_event_loop().time() + self.create_timeout
        while asyncio.get_event_loop().time() < deadline:
            pane = self._tmux("capture-pane", "-t", name, "-p").stdout
            # The same boot dialogs a local worker hits, and nobody is
            # watching this pane either: it sat on "Is this a project you
            # trust?" until the timeout, and reported only that no id had
            # appeared. Clear them the way the local runtime does.
            self._clear_boot_dialog(name, pane)
            match = SESSION_ID.search(pane)
            if match:
                url = ""
                for line in pane.splitlines():
                    if "claude.ai/code" in line:
                        url = line.strip().split()[-1]
                        break
                return match.group(1), url
            if "requires an interactive terminal" in pane:
                raise RuntimeError(
                    "claude refused to start a cloud session: no TTY")
            if "error" in pane.lower() and "session" in pane.lower():
                raise RuntimeError(f"cloud session failed: {pane.strip()[-300:]}")
            await asyncio.sleep(1.0)
        tail = "\n".join(
            line for line in
            self._tmux("capture-pane", "-t", name, "-p").stdout.splitlines()
            if line.strip())[-400:]
        raise RuntimeError(
            f"no cloud session id appeared within {self.create_timeout:.0f}s. "
            f"The pane said:\n{tail or '(nothing)'}")

    def _clear_boot_dialog(self, name: str, pane: str) -> bool:
        """Answer a dialog that is holding the launch. A trust prompt or a
        resume picker blocks before anything is printed, so waiting for the
        id would simply wait forever."""
        low = pane.lower()
        if "trust this folder" in low or "is this a project you" in low:
            self._tmux("send-keys", "-t", name, "Enter")
            return True
        if "resume from summary" in low or "resume full session" in low:
            self._tmux("send-keys", "-t", name, "Enter")
            return True
        return False

    async def send(self, session_id: str, message: str) -> None:
        worker = self._worker(session_id)
        if worker.teleported:
            # Type into the pane: the teleported session is a live claude
            # with no transcript for the local runtime to route through.
            self._tmux("send-keys", "-t", worker.pane, "-l", message)
            self._tmux("send-keys", "-t", worker.pane, "Enter")
            return
        # Non-interactive send is supported; the reply is not returned.
        await self._claude("--cloud", session_id, "--print",
                           "--output-format", "json", message)

    async def teleport(self, session_id: str,
                       working_directory: str | None = None) -> str:
        """Bring a cloud session down to a local pane and start watching it.

        This is what makes a cloud worker supervisable, so it is also what
        "show me that one" should do. Teleport must run from a checkout of
        the session's repository - the CLI refuses otherwise, and says
        which repo it wanted.
        """
        worker = self._worker(session_id)
        if worker.teleported:
            return session_id
        cwd = working_directory or worker.working_directory
        name = f"cond_{worker.task_id}"
        if self._tmux("has-session", "-t", name).returncode == 0:
            self._tmux("kill-session", "-t", name)
        # The stale API key has to go (it 401s, and the pane then never
        # comes up) and stdout stays a TTY. Teleport is interactive and
        # keeps running, so unlike create this pane needs no holding open.
        command = ("env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY "
                   f"{shlex.quote(self.claude)} "
                   f"--teleport {shlex.quote(session_id)}")
        launched = self._tmux("new-session", "-d", "-s", name, "-c", cwd,
                              "sh", "-c", command)
        if launched.returncode != 0:
            raise RuntimeError(f"tmux failed: {launched.stderr.strip()}")
        # Deliberately NOT local.adopt_session: it waits for a transcript
        # file that a teleported session never writes, and hung there until
        # it timed out. Status comes from the provider's feed instead.
        worker.teleported = True
        worker.pane = name
        if worker.watcher is not None:
            worker.watcher.cancel()   # the slow cloud poll is done with
        worker.watcher = asyncio.create_task(self._watch_teleported(worker))
        self._emit(worker, AgentEvent(
            type="progress",
            summary="Teleported from the cloud: you can watch and type into "
                    "it, and its status is readable. Its transcript stays "
                    "server-side, so completions and approvals do not "
                    "arrive as events."))
        return session_id

    async def _watch_cloud(self, worker: _CloudWorker) -> None:
        """Notice when a cloud worker has answered, without being asked.

        Nothing is pushed to us - a cloud session cannot report back - so
        the only way to know is to go and look, and looking is expensive.
        The compromise: look on a slow timer, and treat a changed answer as
        the turn having ended.

        It is a poll, so it is late by up to one interval, and it can miss
        a turn that is superseded before the next look. That is the honest
        ceiling on notifications for work that keeps no local record; a
        worker teleported down gets the fast path instead.
        """
        while not worker.finished and not worker.teleported:
            await asyncio.sleep(self.cloud_poll_seconds)
            if worker.teleported or worker.finished:
                return
            try:
                seen = await self.peek(worker.session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue              # failing to look is not news
            if not seen.get("ok"):
                continue
            said, asked = seen.get("said", ""), seen.get("asked", "")
            if asked:
                # Waiting on the user is not finishing. Announce the
                # question once, and stay quiet about whatever it said
                # before stopping - otherwise a worker stalled on a
                # permission prompt also reports a completion, and the
                # card says done while the work has not started.
                if asked != worker.last_asked:
                    worker.last_asked = asked
                    self._emit(worker, AgentEvent(type="needs_input",
                                                  question=asked))
                continue
            worker.last_asked = ""
            if said and said != worker.last_notified:
                worker.last_notified = said
                self._emit(worker, AgentEvent(type="completed", summary=said))

    async def _watch_teleported(self, worker: _CloudWorker) -> None:
        """Notifications for a session that keeps no local record.

        A cloud session writes no transcript, teleported or not - verified
        by making one answer locally and finding nothing under ~/.claude -
        so the usual event stream does not exist. What does exist is the
        provider's own session feed, and a teleported pane appears in it:
        watching it go running -> idle is the end of a turn.

        That is a completion signal without screen scraping. The summary is
        a different matter: only the pane has what it said, so the last
        lines are read for that and nothing is inferred from them beyond
        being text to show. An empty one still reports the completion.
        """
        while worker.teleported and not worker.finished:
            await asyncio.sleep(self.poll_seconds)
            try:
                row = agent_feed.find(
                    await agent_feed.sessions(claude=self.claude),
                    cwd=worker.working_directory)
                status = agent_feed.status_of(row)
                if status == "disconnected":
                    continue          # not listed yet is not gone
                was, worker.last_status = worker.last_status, status
                if was in ("running", "starting") and status == "idle":
                    self._emit(worker, AgentEvent(
                        type="completed", summary=self._pane_tail(worker)))
            except asyncio.CancelledError:
                raise
            except Exception:
                continue              # a failed poll is not a finished turn

    # What the pane uses to mark things. The assistant's own output is the
    # only part worth quoting back.
    _SAID = "\u23fa"                  # the bullet before an answer
    # The spinner cycles through several glyphs, so all of them count as
    # the end of an answer - one wrong codepoint here and "Brewed for 4s"
    # lands in the card as if the worker had said it.
    _CHROME = ("\u2500",              # the input rule
               "\u276f",              # the prompt
               "\u23f5",              # the mode hint
               "\u273b", "\u273d", "\u2733", "\u2731",   # spinner faces
               "\u25cf",              # the effort/status dot
               "\u2022")

    # Lines the CLI writes about itself. They arrive on the same bullet as
    # an answer, so without this "Session resumed" becomes the summary of
    # a worker that has not spoken since.
    _SYSTEM = ("session resumed", "tmux detected", "auto mode on",
               "context left", "welcome back")

    def _pane_tail(self, worker: _CloudWorker, lines: int = 8) -> str:
        """The last thing the worker actually said, for the card body.

        Taking the last N lines swept up whatever happened to be on screen
        - the spinner's "Brewed for 4s", the input rule, the mode hint.
        The pane marks an answer with its own bullet, so the last such
        block is the answer, and everything after the next piece of chrome
        is not part of it.

        Best effort by construction: this is a screen, not a record. The
        completion it decorates comes from the feed, so a bad read costs a
        line of card text and never the notification.
        """
        if not worker.pane:
            return ""
        pane = self._tmux("capture-pane", "-t", worker.pane, "-p").stdout
        rows = [ln.rstrip() for ln in pane.splitlines()]
        starts = [i for i, ln in enumerate(rows)
                  if ln.lstrip().startswith(self._SAID)]
        # Walk back past the CLI talking about itself to the last thing
        # the worker actually said.
        while starts:
            head = rows[starts[-1]].strip().lstrip(self._SAID).strip().lower()
            if any(head.startswith(m) for m in self._SYSTEM):
                starts.pop()
                continue
            break
        if starts:
            block, first = [], starts[-1]
            for ln in rows[first:]:
                text = ln.strip().lstrip(self._SAID).strip()
                if not text:
                    continue
                if ln is not rows[first] and \
                        ln.strip().startswith(self._CHROME):
                    break             # the answer ended
                if any(text.lower().startswith(m) for m in self._SYSTEM):
                    break
                block.append(text)
                if len(block) >= lines:
                    break
            said = " ".join(block).strip()
            if said:
                return said[:300]
        # Nothing the worker said: fall back to the plainest lines on
        # screen, minus the CLI talking about itself - otherwise a session
        # that has only just resumed reports "Session resumed" as though
        # that were its answer.
        body = []
        for ln in rows:
            text = ln.strip().lstrip(self._SAID).strip()
            if not text or ln.strip().startswith(self._CHROME):
                continue
            if any(text.lower().startswith(m) for m in self._SYSTEM):
                continue
            body.append(text)
        return " ".join(body[-lines:])[:300]

    async def peek(self, session_id: str, timeout: float = 60.0) -> dict:
        """Read a cloud session's current state, without teleporting it.

        The only supported way to see inside a cloud session. Nothing else
        reaches one: --output-format stream-json is refused for --cloud,
        --resume rejects a cloud id, there is no follow flag, `claude
        agents --json` lists local sessions only, and the desktop app keeps
        no local copy - it fetches live and stores nothing.

        But `claude --teleport` into a throwaway checkout brings the
        conversation down as it stands right now, server-side turns
        included, and does NOT mutate it: local turns are already known not
        to propagate back, which is a limitation for working and precisely
        what makes it safe to read. So the probe opens the session in a
        temporary directory, reads what is on screen, and kills it.

        Expensive on purpose - a process and the better part of a minute -
        so this belongs on the moments a user actually asks, never on a
        timer.

        Returns {"said": <last answer>, "asked": <question, if waiting>,
        "ok": bool}; an unreadable session is {"ok": False} rather than an
        exception, because failing to look is not news about the worker.
        """
        worker = self.workers.get(session_id)
        name = f"peek_{session_id[-8:]}"
        tmp = tempfile.mkdtemp(prefix="cloudpeek-")
        subprocess.run(["git", "init", "-q", tmp], capture_output=True)
        Path(tmp, ".keep").write_text("")
        subprocess.run(["git", "-C", tmp, "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", tmp, "-c", "user.email=peek@local",
                        "-c", "user.name=peek", "commit", "-qm", "peek"],
                       capture_output=True)
        if self._tmux("has-session", "-t", name).returncode == 0:
            self._tmux("kill-session", "-t", name)
        command = ("env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY "
                   f"{shlex.quote(self.claude)} "
                   f"--teleport {shlex.quote(session_id)}")
        launched = self._tmux("new-session", "-d", "-s", name, "-c", tmp,
                              "sh", "-c", command)
        if launched.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            return {"ok": False, "said": "", "asked": ""}
        try:
            deadline = asyncio.get_event_loop().time() + timeout
            pane = ""
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(2.0)
                pane = self._tmux("capture-pane", "-t", name, "-p",
                                  "-S", "-60").stdout
                self._clear_boot_dialog(name, pane)
                if "Session resumed" in pane:
                    # The history is drawn around this line, not before it.
                    await asyncio.sleep(3.0)
                    pane = self._tmux("capture-pane", "-t", name, "-p",
                                      "-S", "-60").stdout
                    break
            probe = _CloudWorker(task_id="", session_id=session_id,
                                 working_directory=tmp, pane=name)
            said = self._pane_tail(probe)
            asked = _question_in(pane)
            if worker is not None:
                worker.last_seen = said
            return {"ok": bool(said or asked), "said": said, "asked": asked}
        finally:
            self._tmux("kill-session", "-t", name)
            shutil.rmtree(tmp, ignore_errors=True)

    async def interrupt(self, session_id: str) -> None:
        worker = self._worker(session_id)
        if worker.teleported:
            self._tmux("send-keys", "-t", worker.pane, "Escape")
            return
        raise RuntimeError(
            f"{session_id} is still in the cloud; teleport it first - a "
            "cloud session takes messages but cannot be interrupted from "
            "here")

    async def resume(self, session_id: str,
                     working_directory: str | None = None) -> None:
        # Cloud sessions outlive this process; nothing local to restart.
        return None

    async def get_status(self, session_id: str) -> str:
        worker = self.workers.get(session_id)
        if worker is None:
            return "disconnected"
        if worker.teleported:
            # The provider's feed sees a teleported pane; the transcript
            # watcher never will.
            row = agent_feed.find(await agent_feed.sessions(claude=self.claude),
                                  cwd=worker.working_directory)
            observed = agent_feed.status_of(row)
            return observed if observed != "disconnected" else "running"
        # Blind, and saying so honestly. "running" rather than
        # "disconnected": a cloud session we cannot read is still working,
        # and calling it gone is how a live worker gets replaced.
        return "running"

    async def reconcile_session(self, session_id: str) -> str:
        worker = self.workers.get(session_id)
        if worker is None:
            return "unreachable"
        if worker.teleported:
            status = await self.get_status(session_id)
            return "running" if status in ("running", "starting") else "idle"
        return "unreachable"      # uncertainty, never "missing"

    async def subscribe(self, session_id: str,
                        handler: EventHandler) -> Callable[[], None]:
        worker = self.workers.get(session_id)
        if worker is None:
            raise KeyError(f"unknown cloud session {session_id}")
        worker.handlers.append(handler)

        def unsubscribe() -> None:
            if handler in worker.handlers:
                worker.handlers.remove(handler)
        return unsubscribe

    async def destroy(self, session_id: str) -> None:
        worker = self.workers.pop(session_id, None)
        if worker is not None:
            worker.finished = True
            if worker.watcher is not None:
                worker.watcher.cancel()
        if worker is not None and worker.pane:
            self._tmux("kill-session", "-t", worker.pane)

    def _emit(self, worker: _CloudWorker, event: AgentEvent) -> None:
        for handler in list(worker.handlers):
            try:
                handler(event)
            except Exception:
                pass

    # -- introspection ---------------------------------------------------
    def is_readable(self, session_id: str) -> bool:
        """Whether anything this runtime says about the session is observed
        rather than assumed. False while it is cloud-side."""
        worker = self.workers.get(session_id)
        return bool(worker and worker.teleported)

    def url_for(self, session_id: str) -> str:
        worker = self.workers.get(session_id)
        return worker.url if worker else ""


# A worker that stopped to ask leaves the question on screen; a cloud
# session cannot bypass permissions, so this is the common way one stalls.
_ASKING = ("do you want", "would you like", "may i", "should i",
           "1. yes", "no, exit", "(y/n)", "permission")


def _question_in(pane: str) -> str:
    """The question a worker is waiting on, if it is waiting on one."""
    low = pane.lower()
    if not any(marker in low for marker in _ASKING):
        return ""
    lines = [ln.strip() for ln in pane.splitlines() if ln.strip()]
    for index, line in enumerate(lines):
        if any(m in line.lower() for m in _ASKING):
            return " ".join(lines[max(0, index - 2):index + 3])[:300]
    return ""


def _default_browser() -> str:
    """The bundle id handling https, so a session URL can be sent to it by
    name instead of to whatever claimed the domain."""
    import plistlib
    prefs = (Path.home() / "Library/Preferences/com.apple.LaunchServices"
             / "com.apple.launchservices.secure.plist")
    try:
        handlers = plistlib.loads(prefs.read_bytes()).get("LSHandlers", [])
    except Exception:
        return ""
    for handler in handlers:
        if handler.get("LSHandlerURLScheme") == "https":
            return str(handler.get("LSHandlerRoleAll") or "")
    return ""


def _desktop_app_handles_code_sessions() -> bool:
    """Whether Claude for Mac can actually show a code session.

    It cannot today: no claude:// route reaches one, and an unconfigured
    app shows its sign-in screen instead. Kept as one honest check rather
    than a hardcoded False, so that the day the app gains a route this
    turns on by noticing rather than by someone remembering.
    """
    app = Path("/Applications/Claude.app")
    if not app.is_dir():
        return False
    try:
        plist = (app / "Contents/Resources/app.asar").read_bytes()
    except OSError:
        return False
    # A route for code sessions would have to name them somewhere.
    return b"claude://code" in plist or b"handleCodeSessionDeepLink" in plist


def _clean_env() -> dict:
    """A stale ANTHROPIC_API_KEY takes the API path and 401s; the stored
    claude.ai login is what a cloud session runs on."""
    import os
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    # The voice's key; see tmux_runtime.CONDUCTOR_SECRETS.
    env.pop("OPENAI_API_KEY", None)
    return env
