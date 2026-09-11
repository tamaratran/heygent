"""Live end-to-end check of the visible Boss on a real CLI - Claude Code
or Codex - with a real conductor, bridge, boss-mcp and CLI, in a PRIVATE
tmux server (`tmux -L vcboss`) the live conductor never lists, and a
scratch home. Uses the real accounts, so it is not part of the suite.

    python -u tests/smoke_boss_cli.py codex [CHECK...]

with the interpreter AGENTS.md's "Verify" section builds (the whole
suite's dependencies: conduct.py is imported).

CLI: codex | claude-code.  checks: warm tools prose queued worker resume
(default: all; worker starts a real Claude Code worker and takes minutes).

Real: GlobalConductor, BossBridge, boss-mcp (uv), PtyManagerBackend,
TmuxClaudeRuntime and the CLI. Not real: no voice, no overlay, no
surfaces (nothing opens a window, nothing takes focus), the computer-use
probe is stubbed, and conduct.py's supervisory branch is copied in
trimmed. Paths are short on purpose: a unix socket path must fit in 104
bytes, and Claude Code shortens a project folder name past 200.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
CLI = sys.argv[1] if len(sys.argv) > 1 else "codex"
WANTED = set(sys.argv[2:]) or {"warm", "tools", "prose", "queued", "worker",
                               "resume"}
BASE = Path(os.environ.get("BOSS_SMOKE_DIR", "/private/tmp/vc-boss-smoke"))
RUN = BASE / CLI
HOME = RUN / "home"
PROJ = RUN / "projects" / "owlery"
SOCK = BASE / f"{CLI}.sock"
T = [shutil.which("tmux") or "tmux", "-L", "vcboss"]
RESULTS: list[dict] = []
# Every tmux the runtime runs goes to the private server.
_BIN = BASE / "bin"
_BIN.mkdir(parents=True, exist_ok=True)
(_BIN / "tmux").write_text(f'#!/bin/sh\nexec {T[0]} -L vcboss "$@"\n')
(_BIN / "tmux").chmod(0o755)
os.environ["PATH"] = f"{_BIN}:{os.environ['PATH']}"
os.environ.pop("TMUX", None)

from conductor import tmux_runtime, capabilities          # noqa: E402
tmux_runtime.TERMINALS_DIR = RUN / "terminals"
tmux_runtime.STREAMS_DIR = RUN / "terminals" / "streams"
capabilities.computer_state = lambda: (False, "not probed in this test")

import conduct                                              # noqa: E402
from conductor.boss_bridge import BossBridge                # noqa: E402
from conductor.claude_manager import _serialize             # noqa: E402
from conductor.observability import configure_logging       # noqa: E402
from conductor.pty_manager import PtyManagerBackend         # noqa: E402
from conductor.supervisor_inbox import SupervisorInbox, SupervisoryEvent  # noqa: E402
from conductor.app_web import CodexWeb                      # noqa: E402


def record(name, ok, detail=""):
    RESULTS.append({"cli": CLI, "check": name, "ok": ok,
                    "detail": str(detail)[:600]})
    print(f"[{'PASS' if ok else 'FAIL'}] {CLI}: {name} {detail}".rstrip(),
          flush=True)


def fresh_project():
    PROJ.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=PROJ)
    (PROJ / "README.md").write_text("# Owlery\nA tiny test project.\n")
    subprocess.run(["git", "add", "."], cwd=PROJ)
    subprocess.run(["git", "-c", "user.email=e@x", "-c", "user.name=e2e",
                    "commit", "-qm", "init"], cwd=PROJ)


class World:
    """A conductor, as conduct.py wires one, minus voice and windows."""

    def __init__(self):
        self.events = []
        self.prose = []            # (monotonic, text)
        self.page = []             # what the window's page was pushed
        self.interims = []

    async def build(self):
        configure_logging(HOME)
        self.conductor = conduct.build_conductor(
            HOME, [str(PROJ.parent)], worker_surface="terminal",
            boss_mode="visible", boss_transport="stdio",
            embedded_terminals=True, boss_cli=CLI)
        self.conductor.locator.register(str(PROJ))
        self.conductor.bus.subscribe(self._on_bus)
        self.inbox = SupervisorInbox(HOME / "supervisor_inbox.jsonl")
        self.conductor.inbox = self.inbox
        self.bridge = BossBridge(SOCK, self.conductor, _serialize)
        await self.bridge.start()
        self.attach(self.conductor.manager)
        await self.conductor.startup()

    def attach(self, manager: PtyManagerBackend):
        manager.bridge_socket = SOCK
        manager.turn_timeout = 240.0
        manager.attach_bridge(self.bridge)
        page = types.SimpleNamespace(busy=True, push=self.page.append)

        def on_prose(text):
            self.prose.append((time.monotonic(), text))
            CodexWeb.mirror_delta(page, text)       # the window's own code
        manager.on_prose = on_prose
        manager.on_interim = self.interims.append
        self.manager = manager

    def _on_bus(self, event):
        self.events.append(event)
        if event.type == "subagent.state_changed":
            self._offer(event)

    def _offer(self, event):
        """conduct.py's offer_supervisory, trimmed to what reaches the Boss."""
        state = event.data.get("state") or {}
        transition = event.data.get("transition") or {}
        if not state.get("task_id") or \
                transition.get("outcome") not in ("applied", "lifecycle"):
            return
        sup = None
        if transition.get("kind") == "completed" and \
                "result" in transition.get("changed", []):
            sup = ("completed", (state.get("result") or {}).get("summary", ""))
        elif state.get("status") == "failed" and transition.get("from") != "failed":
            sup = ("failed", (state.get("result") or {}).get("summary", ""))
        if sup is None:
            return
        _, task = self.conductor._find_task(state["task_id"])
        supervisory = SupervisoryEvent(
            event_id=f"{transition.get('event_id', '')}:{sup[0]}",
            task_id=task.id, subagent_id=f"sub_{task.id}", type=sup[0],
            summary=sup[1], requires_action=False, trace_id=event.trace_id,
            source_event_id=event.event_id, task_title=task.title,
            project_name="Owlery")
        if self.inbox.offer(supervisory):
            self.manager.record_supervisory(supervisory)
            self.manager.deliver_supervisory(supervisory)

    def of(self, kind):
        return [e for e in self.events if e.type == kind]

    def timeline(self):
        store = self.manager.store
        return store.events(self.manager.session.id)

    def pane(self):
        return subprocess.run(T + ["capture-pane", "-p", "-t", "cond_boss"],
                              capture_output=True, text=True).stdout


async def wait_for(pred, timeout, step=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return pred()


async def check_warm(w: World):
    started = time.monotonic()
    ok = await w.manager.warm(w.conductor)
    record("warm: Boss opened with its tools", ok and w.manager.session.status == "ready",
           f"{time.monotonic() - started:.1f}s status={w.manager.session.status}")
    boss_dir = HOME / "boss"
    if CLI == "codex":
        launcher = boss_dir / "boss-mcp-codex"
        mode = oct(launcher.stat().st_mode & 0o777)
        record("warm: AGENTS.md written, launcher 0700", (boss_dir / "AGENTS.md").exists()
               and mode == "0o700", mode)
        token = w.manager._credential
        ps = subprocess.run(["ps", "-axww", "-o", "command"], capture_output=True,
                            text=True).stdout
        codex_lines = [l for l in ps.splitlines() if str(launcher) in l]
        record("warm: credential not on any command line",
               bool(codex_lines) and token not in ps, f"{len(codex_lines)} codex processes name the launcher")
        record("warm: provider id unknown until first message",
               w.manager.session.provider_session_id is None,
               w.manager.session.provider_session_id)
        leaked = [s for s in ("computer-use", "node_repl", "cua_repl")
                  if any(f"mcp_servers.{s}.enabled=false" in l for l in codex_lines)]
        record("warm: user MCP servers switched off", "node_repl" in leaked, leaked)
    else:
        record("warm: CLAUDE.md written", (boss_dir / "CLAUDE.md").exists())


async def check_tools(w: World):
    t0 = time.monotonic()
    turn = await w.manager.handle(
        "Which projects do I have registered? Look it up with your tools, "
        "then answer in one short sentence.", w.conductor)
    took = time.monotonic() - t0
    tools = [c.tool for c in turn.tool_calls]
    record("tools: boss tool called over MCP", bool(tools), tools)
    record("tools: reply read off the transcript", "owlery" in turn.reply.lower(),
           f"{took:.1f}s {turn.reply[:200]!r}")
    kinds = [e.type for e in w.timeline()]
    record("tools: timeline has user, tool, reply",
           "user_message" in kinds and "tool_completed" in kinds
           and "boss_message" in kinds, kinds[-8:])
    pid = w.manager.session.provider_session_id
    if CLI == "codex":
        from conductor.codex_adapter import CodexAdapter
        path = CodexAdapter().transcript_for(str(HOME / "boss"), pid or "none")
        record("tools: Codex id recorded from the first rollout",
               bool(pid) and path is not None, f"{pid} {path}")
    else:
        record("tools: session id recorded", bool(pid), pid)
    stored = json.loads((HOME / "boss" / "sessions" / w.manager.session.id /
                         "session.json").read_text())
    record("tools: id persisted to session.json", stored.get("provider_session_id") == pid)


async def check_prose(w: World):
    before = len(w.prose)
    t0 = time.monotonic()
    task = asyncio.create_task(w.manager.handle(
        "First say one short sentence about what you are about to do, then call "
        "list_open_sessions, then answer with exactly three short paragraphs about "
        "owls separated by blank lines. No lists, no headings.", w.conductor))
    turn = await task
    done = time.monotonic()
    got = w.prose[before:]
    record("prose: paragraphs reached the window before the turn settled",
           bool(got) and got[0][0] < done - 1.0,
           [f"+{t - t0:.1f}s {x[:60]!r}" for t, x in got] + [f"answer +{done - t0:.1f}s"])
    last = got[-1][1] if got else ""
    record("prose: the final message whole, paragraphs kept",
           last.count("\n\n") >= 2 and " ".join(last.split()) in " ".join(turn.reply.split()),
           f"breaks={last.count(chr(10) * 2)} reply={len(turn.reply)} prose={len(last)}")
    deltas = [m for m in w.page if m.get("kind") == "delta"]
    record("prose: CodexWeb.mirror_delta pushed delta messages", len(deltas) >= len(got) > 0,
           len(deltas))
    if len(got) > 1:
        record("prose: a mid-turn line came before the tool finished",
               any(e.type == "tool_completed" for e in w.timeline()), got[0][1][:80])


async def check_queued(w: World):
    first = asyncio.create_task(w.manager.handle(
        "Call the situation tool and tell me in one sentence what is going on.",
        w.conductor))
    await asyncio.sleep(2.5)
    second = asyncio.create_task(w.manager.handle(
        "Also: what is seven times six? Answer both in one reply.", w.conductor))
    a, b = await asyncio.gather(first, second)
    both = (a.reply + " " + b.reply).lower()
    lost = [e for e in w.timeline() if "never read" in str(e.payload.get("text", ""))]
    record("queued: words typed mid-turn were read and answered",
           ("42" in both or "forty-two" in both) and not lost,
           f"a={a.reply[:120]!r} folded={a.folded} b={b.reply[:120]!r} folded={b.folded}")
    record("queued: neither turn timed out",
           "still in progress" not in both, "")


async def check_worker(w: World):
    turn = await w.manager.handle(
        "Start one worker in the Owlery project with this goal: create a file "
        "named hello.txt containing the single word hi, do not commit, then "
        "reply with the word done. Do not open or focus any window. Tell me "
        "in one sentence once it is started.", w.conductor)
    tools = [c.tool for c in turn.tool_calls]
    record("worker: create_task through the Boss", "create_task" in tools,
           f"{tools} {turn.reply[:150]!r}")
    if "create_task" not in tools:
        return
    pushed = await wait_for(lambda: any(
        e.type == "system_event" and e.payload.get("kind") == "worker_update"
        for e in w.timeline()), 360, 2.0)
    record("worker: its finish was typed into the Boss", pushed)
    told = await wait_for(lambda: any(
        e.data.get("source") == "worker_update" for e in w.of("boss.tell_user")), 120, 1.0)
    said = [e.data.get("text") for e in w.of("boss.tell_user")]
    record("worker: the Boss's reply to it went to the voice (tell_user)", told, said[-1:])
    worktrees = list((HOME / "workspaces").rglob("hello.txt"))
    record("worker: the worker really did the work", bool(worktrees), worktrees[:1])


async def check_resume(w: World):
    old = w.manager
    pid = old.session.provider_session_id
    subprocess.run(T + ["kill-session", "-t", "cond_boss"], capture_output=True)
    needle = str(HOME / "boss" / ("boss-mcp-codex" if CLI == "codex" else "mcp.json"))

    def cli_alive():
        # Not old._process_running(): a tmux server keeps the argv of the
        # first session it started, so it matches after that session died.
        ps = subprocess.run(["ps", "-axww", "-o", "command"], capture_output=True,
                            text=True).stdout.splitlines()
        return [l for l in ps if needle in l and "tmux" not in l.split()[0]]
    gone = await wait_for(lambda: not cli_alive(), 20, 0.5)
    record("resume: old Boss process ended", gone)
    await old.close()
    # A restart: a new backend on the same store and runtime.
    fresh = conduct.build_boss(w.conductor.runtime, HOME, "visible", "stdio", CLI)
    w.attach(fresh)
    w.conductor.manager = fresh
    t0 = time.monotonic()
    turn = await fresh.handle(
        "What was the very first thing I asked you in this conversation? "
        "One sentence, no tools.", w.conductor)
    record("resume: same provider session resumed",
           fresh.session.provider_session_id == pid and any(
               "resumed" in str(e.payload.get("text", "")) for e in w.timeline()),
           f"{pid} -> {fresh.session.provider_session_id}")
    record("resume: reply read from the resumed transcript",
           "project" in turn.reply.lower() and "still in progress" not in turn.reply.lower(),
           f"{time.monotonic() - t0:.1f}s {turn.reply[:200]!r}")
    after = len(w.prose)
    turn2 = await fresh.handle("Say the word pineapple and nothing else.", w.conductor)
    record("resume: a second turn after resume", "pineapple" in turn2.reply.lower(),
           turn2.reply[:80])
    record("resume: prose still streams after resume", len(w.prose) > after)


async def main():
    if RUN.exists():
        shutil.rmtree(RUN)
    SOCK.unlink(missing_ok=True)
    HOME.mkdir(parents=True)
    fresh_project()
    subprocess.run(T + ["kill-server"], capture_output=True)
    w = World()
    await w.build()
    try:
        for name, fn in (("warm", check_warm), ("tools", check_tools),
                         ("prose", check_prose), ("queued", check_queued),
                         ("worker", check_worker), ("resume", check_resume)):
            if name not in WANTED:
                continue
            try:
                await fn(w)
            except Exception as exc:
                record(f"{name}: raised", False, f"{exc!r}")
                traceback.print_exc()
    finally:
        (RUN / "pane.txt").write_text(w.pane())
        (RUN / "timeline.json").write_text(json.dumps(
            [e.to_dict() for e in w.timeline()] if w.manager.session else [], indent=1))
        (BASE / f"results-{CLI}.json").write_text(json.dumps(RESULTS, indent=1))
        for task in w.conductor.list_tasks():
            try:
                await w.conductor.cancel_task(task.id)
            except Exception:
                pass
        await w.bridge.stop()
        subprocess.run(T + ["kill-server"], capture_output=True)
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
