#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "aiohttp>=3.10,<4",
#   "numpy>=1.26,<3",
#   "sounddevice>=0.4.6,<1",
#   "claude-agent-sdk>=0.2,<1",
#   "mcp>=2,<3",
#   # Computer use: the conductor probes the macOS GUI permissions through
#   # Quartz and ApplicationServices, so the bindings come with the app the
#   # way tmux/cmux setup is documented rather than left to the user.
#   "pyobjc-framework-Quartz>=10,<13; sys_platform == 'darwin'",
#   "pyobjc-framework-ApplicationServices>=10,<13; sys_platform == 'darwin'",
#   # The startup permission check probes the Microphone grant through
#   # AVFoundation, the same way computer use probes its two.
#   "pyobjc-framework-AVFoundation>=10,<13; sys_platform == 'darwin'",
# ]
# ///

"""The Voice Conductor: hold Fn, talk, and manage many coding agents at once.

Same push-to-talk front end as voice_agent.py - GPT Live is the ears and the
mouth - but every request lands in conductor.handle_user_message(), where a
Claude Manager session routes it to the right task:

    "Fix the login redirect"                -> create_task
    "Tell the login one not to touch OAuth" -> send_to_task(task_login)
    "How's everything going?"               -> list_tasks
    "Stop the settings one"                 -> interrupt_task(task_settings)

Tasks persist under ~/.voice-conductor/projects/<id>/ and survive restarts;
startup reconnects what it can and reports what it cannot.

    ./conduct.sh                          # manage tasks in the current repo
    ./conduct.sh --register ~/code/app    # ... or another project
                                          # git worktrees when possible

Voice does no routing (spec Invariant 8): the same handle_user_message()
call the tests drive is all this file uses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import shlex
import signal
import subprocess
import shutil
import sys
from pathlib import Path

import boss
import voice_agent
from voice_agent import (HotkeyListener, Ui, VoiceAgent, ask_for_api_key,
                         load_env, log, withhold_api_key)

from conductor import (JsonlSink, LoggingSink, ObservabilityBus,
                       application_log, configure_logging, current_run,
                       drain_subprocess_stderr, instance,
                       install_asyncio_exception_handler,
                       start_loop_stall_monitor)
from conductor.claude_manager import ClaudeManagerBackend
from conductor.boss_bridge import BossBridge
from conductor.conductor_mcp import ConductorMcp
from conductor.boss_session import BossSessionStore, render_timeline
from conductor.pty_manager import PtyManagerBackend
from conductor.global_conductor import GlobalConductor
from conductor.gui_permissions import ask_for_missing_grants
from conductor.plain_text import plain_text
from conductor.notifications import (NotificationService,
                                     TaskNotification, concise)
from conductor.notification_panel import notice_payload
from conductor.session_card import project_card
from conductor.supervisor_inbox import (SpeechPolicy, SupervisoryEvent,
                                        SupervisorInbox, event_id_for)

# How long the voice waits after a supervisory event before deciding what
# to say, so three finishes landing together become one sentence rather
# than three. Short enough that a lone finish is not noticeably late.
SPEECH_BEAT_S = 1.5
from conductor.voice_coordinator import VoiceCoordinator
from conductor.projects import DEFAULT_HOME
from conductor.surfaces import InteractiveTerminalSurface

HERE = Path(__file__).resolve().parent
boss_mod = boss


def _load_prompt(name: str, fallback: str) -> str:
    try:
        text = (HERE / "prompts" / name).read_text()
    except OSError:
        application_log("conductor", "prompt.load_failed",
                        f"falling back to the built-in {name} prompt",
                        severity="warning", exc_info=True, prompt=name)
        return fallback
    _, _, body = text.partition("\n---\n")
    return (body or text).strip() or fallback


class ConductorVoice(VoiceAgent):
    """The work seam is the Manager, not a single Claude session."""

    def __init__(self, api_key: str, ui: Ui, conductor: GlobalConductor,
                 reply_mode: str = "both") -> None:
        # The conductor's own bus, so the microphone and the routing it
        # causes reach one sink and read back as one trace.
        super().__init__(api_key, ui, allow_write=False,
                         reply_mode=reply_mode, bus=conductor.bus)
        self.conductor = conductor
        self.boss_interim = ""      # already spoken; the answer need not repeat it
        # The Boss window's page (CodexWeb), when one is open: spoken
        # turns are drawn there too, as they happen.
        self.mirror = None

    def _reply_spoken(self, text: str) -> None:
        # The Boss learns what the voice said through what_the_voice_said,
        # when it asks; nothing is typed into its window for this.
        try:
            self.conductor.voice_spoke(text)
        except Exception:
            application_log("voice", "voice.reply_record_failed",
                            "could not keep what the voice said",
                            severity="warning", exc_info=True)

    async def _work(self, prompt: str, trace_id: str = "") -> str:
        """One utterance -> one Manager turn -> its spoken reply.

        Every completed utterance lands here directly: the voice side
        makes no delegate-or-not decision. What a second thing said
        mid-turn does is the backend's call: the SDK Boss waits and gives
        it its own turn (ClaudeManagerBackend); the visible Boss types the
        words in at once and may read them into the running turn,
        answering both at once on the FIRST item and marking this one
        folded - nothing to say here (PtyManagerBackend).

        Whatever happens, an answer comes back and the caller closes the
        work item, so a turn that is taking too long is reported as still
        in progress and its answer spoken later, rather than left hanging.
        """
        # What the microphone heard behind this prompt, read before the
        # first await: the next work item overwrites it. The base class
        # attached these words to the prompt; the conductor also holds the
        # manager to them when it forwards a follow-up.
        spoken = self.last_utterance
        print(f"  → manager: {prompt}", flush=True)
        log("manager_turn", text=prompt)
        turn_trace = trace_id or self.trace_id
        application_log("manager", "manager.turn_started", prompt[:300],
                        trace_id=turn_trace)
        if self.conductor.manager_busy:
            print("     (the Boss is mid-turn; these words go in now)",
                  flush=True)
        self._mirror_prompt(prompt)
        shown = None
        work = asyncio.ensure_future(self.conductor.handle_user_message(
            prompt, source="voice", trace_id=turn_trace, utterance=spoken))
        try:
            # Shielded: a timeout stops the waiting, not the turn. Cancelling
            # the turn would leave the Manager mid-conversation, and the
            # next message would land in the middle of it.
            turn = await asyncio.wait_for(asyncio.shield(work),
                                          timeout=boss.MANAGER_TURN_TIMEOUT_S)
            for call in turn.tool_calls:
                gist = json.dumps(call.args)[:70]
                print(f"     ⚒ {call.tool}({gist})", flush=True)
                log("manager_tool", tool=call.tool, args=call.args)
            if turn.folded:
                # Answered together with what the user said before it,
                # on that item. An empty answer closes this one without
                # a word (voice_agent._run_claude).
                answer = ""
            else:
                answer = turn.reply or "Done."
            # What the Boss noted for the voice's ears only; delivered
            # on the commentary channel when the delegation closes.
            self.pending_commentary = getattr(turn, "commentary", "") or ""
            # The window shows the whole reply: it already drew the first
            # sentence as it was written, and the trim below is for ears.
            shown = answer
            said, self.boss_interim = self.boss_interim, ""
            if said:
                # The first sentence was spoken while the tools ran; what
                # follows it is the news. When it was the whole answer,
                # nothing follows - and an empty answer here means "already
                # said", never "say it all again". Measured 2026-08-30
                # 23:34:44Z: interim and final 4 ms apart, the same
                # paragraph spoken twice, every slow turn.
                for form in (answer, plain_text(answer)):
                    if form.startswith(said[:60]):
                        answer = form[len(said):].lstrip(" .")
                        break
        except asyncio.TimeoutError:
            answer = "That is still in progress. I'll say when it's done."
            application_log("manager", "manager.turn_timeout",
                            "the Manager turn outlived the voice side's "
                            "wait; its answer will be announced when it "
                            "arrives", severity="warning",
                            trace_id=turn_trace, prompt=prompt[:300],
                            timeout_s=boss.MANAGER_TURN_TIMEOUT_S)
            work.add_done_callback(
                lambda done: asyncio.ensure_future(
                    self._late_answer(prompt, done, turn_trace)))
        except Exception as exc:
            answer = f"That did not work: {exc}"
            application_log("manager", "manager.turn_failed",
                            "Manager turn failed", severity="error",
                            exc_info=True, trace_id=turn_trace,
                            prompt=prompt[:300])
        self._mirror_answer(shown if shown is not None else answer)
        answer = answer[:voice_agent.ANSWER_CHAR_LIMIT]
        print(f"  ← manager: {answer or '(answered with the earlier words)'}",
              flush=True)
        log("manager_answer", text=answer)
        return answer

    def _mirror_prompt(self, prompt: str) -> None:
        if self.mirror is None:
            return
        try:
            self.mirror.mirror_prompt(prompt)
        except Exception:
            application_log("ui", "boss_ui.mirror_failed",
                            "the Boss window missed a spoken turn",
                            severity="warning", exc_info=True)

    def _mirror_answer(self, answer: str) -> None:
        if self.mirror is None or not answer:
            return
        try:
            self.mirror.mirror_answer(answer)
        except Exception:
            application_log("ui", "boss_ui.mirror_failed",
                            "the Boss window missed an answer",
                            severity="warning", exc_info=True)

    async def _late_answer(self, prompt: str, done: asyncio.Future,
                           trace_id: str) -> None:
        """A turn that outlived the voice side's wait still reaches the
        user, by the session's own voice."""
        try:
            turn = done.result()
        except asyncio.CancelledError:
            return
        except Exception:
            application_log("manager", "manager.turn_failed",
                            "Manager turn failed after the voice side "
                            "stopped waiting", severity="error", exc_info=True,
                            trace_id=trace_id, prompt=prompt[:300])
            return
        if turn.folded:
            return                  # answered on the earlier item
        self._mirror_answer(turn.reply or "Done.")
        answer = (turn.reply or "Done.")[:voice_agent.ANSWER_CHAR_LIMIT]
        print(f"  ← manager (late): {answer}", flush=True)
        log("manager_answer", text=answer, late=True)
        await self.announce(f"About \"{prompt[:80]}\": {answer}")


def notes_between_turns(agent):
    """The bus handler for a note the Boss made with no voice turn open.

    A turn's notes ride back with the turn (ManagerTurn.commentary); a
    note made between turns - the Boss replying to a pushed worker
    update - has nothing to ride and used to be dropped. It goes to the
    voice model now, on the commentary channel, never spoken: the voice
    knows the answer the moment the Boss does, and says it in its own
    words when the user asks, or when tell_user follows.
    """
    def handler(event) -> None:
        if event.type != "boss.note_for_voice" or \
                not (event.data or {}).get("outside_turn"):
            return
        text = " ".join(str(event.data.get("text", "")).split())
        if text:
            asyncio.create_task(agent._session_commentary(text))
    return handler


def raise_boss_window(pid: int) -> None:
    """Put the voice-agent window in front - its process, by pid.

    The pid we hold is uv's wrapper, and the wrapper owns no window:
    System Events can only front the descendant that does (measured
    2026-09-01 - window_show_failed on every new chat, while fronting
    the child by hand worked). Walk down to the deepest descendant."""
    try:
        while True:
            kids = subprocess.run(["pgrep", "-P", str(pid)],
                                  capture_output=True, text=True, timeout=5)
            child = next((int(word) for word in kids.stdout.split()
                          if word.isdigit()), None)
            if child is None or child == pid:
                break
            pid = child
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to set frontmost of '
         f'(first process whose unix id is {pid}) to true'],
        check=True, capture_output=True, timeout=10)


def chosen_surface(override: str | None = None) -> str:
    """Where managed workers are visible when nothing chose for them.

    With the Boss window open the workers live in plain tmux sessions and
    the window draws their terminals itself, so main() picks terminal
    before this is asked. Without the window (or when the caller never
    says), cmux remains the answer.
    """
    return override or getattr(boss, "WORKER_SURFACE", "cmux")


def build_boss(runtime, home: Path, mode: str, transport: str = "stdio"):
    """The Boss: a visible session in the worker surface, or the invisible
    SDK call it used to be.

    "visible" hosts the Boss the way a worker is hosted - a claude process
    in its own cmux workspace, its tools served by the conductor, its
    every turn and tool call written to a BossSession timeline. The window
    is the execution; there is no hidden Boss behind it. "headless" is the
    old backend, kept for a machine with no cmux and for comparison.

    transport is how the visible Boss reaches its tools: "http" is the
    conductor's own loopback MCP endpoint (nothing else runs); "stdio" is
    boss-mcp, the packaged helper, over the bridge socket.
    """
    if mode != "visible":
        return ClaudeManagerBackend()
    local = getattr(runtime, "runtimes", {}).get("local", runtime)
    return PtyManagerBackend(
        local, home, home / "boss" / "tools.sock",
        python=sys.executable, repo_root=HERE,
        turn_timeout=getattr(boss_mod, "MANAGER_TURN_TIMEOUT_S", 300.0),
        store=BossSessionStore(home), transport=transport,
        session_settings=toast_hook(home))


def toast_python() -> str | None:
    """An interpreter that will still be there tomorrow, or None.

    conductor/turn_toast.py is stdlib-only on purpose, so any python3
    runs it - which matters, because sys.executable is not always a
    lasting path. `uv run --with ...` builds a temporary environment and
    then removes it, and a hook whose interpreter has gone puts

        Stop hook error: Failed with non-blocking status code: /bin/sh:
        .../uv/builds-v0/.tmpXXXX/bin/python: No such file or directory

    under EVERY turn the Boss takes - measured 2026-08-30. Noise under
    every turn is worse than no toast, so a build-env interpreter is
    passed over, and if nothing lasting is found the Boss is launched
    with no hook at all.
    """
    for candidate in (sys.executable, shutil.which("python3"),
                      "/usr/bin/python3"):
        if not candidate or "/.cache/uv/builds" in candidate:
            continue
        if Path(candidate).exists():
            return candidate
    return None


def toast_hook(home: Path) -> dict:
    """Claude Code settings for THIS Boss session: the Stop hook that
    puts a linked toast under a turn that mentions a session.

    --settings, so nothing is written to the user's own settings files
    and no other Claude Code session on the machine grows a hook. See
    conductor/turn_toast.py for what the hook may and may not draw.
    """
    python = toast_python()
    if python is None:
        application_log("ui", "toast.no_interpreter",
                        "no lasting python3 for the turn-toast hook; the "
                        "Boss runs without it", severity="warning")
        return {}
    command = " ".join(shlex.quote(part) for part in (
        python, str(HERE / "conductor" / "turn_toast.py"), str(home)))
    return {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": command, "timeout": 10}]}]}}


def build_conductor(home: Path,
                    search_roots: list[str],
                    worker_location: str | None = None,
                    worker_surface: str | None = None,
                    boss_mode: str = "visible",
                    boss_transport: str = "stdio",
                    embedded_terminals: bool = False) -> GlobalConductor:
    """Workers are always PTY-hosted: the window IS the live session, so it
    can be watched and typed into while the Conductor supervises it.

    There was a second, SDK-hosted runtime behind an --interactive flag. Two
    execution models meant two sets of behaviour to reason about for every
    feature, and the headless one could only ever be watched, never joined.
    """
    bus = ObservabilityBus()
    bus.subscribe(JsonlSink(home))
    # Domain events also reach the one debugging log, so a failure and the
    # events around it read as a single ordered story.
    bus.subscribe(LoggingSink())
    # Where a worker's PTY lives. cmux is a host, not a view: whoever
    # owns the PTY owns the execution, so this cannot be a surface layered
    # over tmux - resuming the session into cmux while a tmux pane still
    # held it would be two processes on one conversation.
    if chosen_surface(worker_surface) == "cmux":
        from conductor.cmux_runtime import CmuxClaudeRuntime as _Runtime
    else:
        from conductor.tmux_runtime import TmuxClaudeRuntime as _Runtime
    runtime = _Runtime(
        bus=bus, transcript_dir=str(home / "executions"),
        permission_mode=boss.WORKER_PERMISSION_MODE)
    # Both places are always available; the default only says which one an
    # unqualified "start a task" means. Building one runtime per launch was
    # what made this a file edit and a restart.
    from conductor.cloud_runtime import CloudClaudeRuntime
    from conductor.routing_runtime import RoutingRuntime
    default = worker_location or getattr(boss, "WORKER_LOCATION", "local")
    # One more runtime per other CLI on this machine: the same host
    # (cmux or tmux) with that CLI's adapter. Only the ones that are
    # actually installed; capabilities tells the Boss which those are.
    from conductor.cli_adapter import CliAdapter, adapter_for
    from conductor.capabilities import PROVIDER_BINARIES
    providers = {}
    for name, binaries in PROVIDER_BINARIES.items():
        if name == "claude-code":
            continue
        found = next((path for binary in binaries
                      if (path := shutil.which(binary))), None)
        if not found:
            continue
        adapter = adapter_for(name, found)
        if type(adapter) is CliAdapter:
            continue            # installed, but we know nothing about it yet
        providers[name] = _Runtime(
            bus=bus, transcript_dir=str(home / "executions"),
            permission_mode=boss.WORKER_PERMISSION_MODE, adapter=adapter)
    runtime = RoutingRuntime(
        local=runtime,
        cloud=CloudClaudeRuntime(
            local=runtime,
            prefer_app=getattr(boss, "CLOUD_PREFER_APP", False),
            cloud_poll_seconds=getattr(boss, "CLOUD_POLL_SECONDS", 90.0)),
        default=default, providers=providers)
    # Only surfaces the user can actually work in. TranscriptSurface opens a
    # Terminal window tailing the log: it shows the right session but typing
    # reaches nothing, so as a fallback it produced windows that looked live
    # and were not. If the interactive attachment fails, no window is better
    # than a dead one - the failure is reported instead.
    if embedded_terminals:
        # The Boss window draws every worker's terminal itself, so there
        # is no external window to open: a Terminal.app window here would
        # be a second copy of a session the window already shows.
        surfaces = {}
        preference = None
    elif chosen_surface(worker_surface) == "cmux":
        # The worker lives in a cmux workspace, so attaching means finding
        # that workspace - not opening a Terminal window onto a tmux pane
        # that does not exist.
        from conductor.cmux_surface import CmuxSurface
        from conductor.surfaces import SurfacePreference
        surfaces = {"cmux": CmuxSurface()}
        # And it has to be ASKED for. The Conductor walks the provider's
        # preference order and picks the first registered name; the
        # default order names interactive-terminal and claude-app, so a
        # surface registered under any other name is never reached - the
        # click fails with no surface rather than the wrong one.
        preference = SurfacePreference(claude_code=("cmux",),
                                       codex=("cmux",))
    else:
        surfaces = {"interactive-terminal": InteractiveTerminalSurface()}
        preference = None
    # No project is opened or selected: the Manager resolves projects from
    # the registry and, failing that, the locator's search roots.
    built = GlobalConductor(home=home, runtime=runtime, bus=bus,
                            search_roots=search_roots,
                            surfaces=surfaces,
                            surface_preference=preference,
                            idle_retire_s=getattr(boss, "IDLE_RETIRE_S", 0.0),
                            manager=build_boss(runtime, home, boss_mode,
                                               boss_transport))
    if isinstance(built.manager, PtyManagerBackend):
        built.boss_store = built.manager.store
    return built


async def preflight_surface(worker_surface: str | None = None,
                            install_missing: bool = True) -> str:
    """Make the surface work; fall back to tmux when it will not.

    Returns the surface the launch should actually use. cmux being the
    default means a machine where cmux cannot be installed, launched or
    driven must still start: workers land in tmux panes - every one of
    them still a window the user can open and type into (section 8 is
    about visibility, not about which app draws the window) - and the
    console says so and says why. Only an EXPLICIT --worker-surface cmux
    still refuses, because then the user asked for cmux by name.

    Only when cmux is actually selected: a dependency nothing uses is not
    a dependency. But cmux IS the default now, and "not usable" has three
    causes that are ours to fix rather than the user's to read about -
    it is not installed, it is not running (the control socket exists only
    while the app does), or it is not configured to accept socket control.
    Printing brew commands and exiting for any of those left the product
    unstartable on a machine that had simply never seen cmux.

    Configuring access runs even when cmux looks fine, because cmux
    rewrites its own config from a template on some launches: that dropped
    the password mid-session and stopped the app booting at all.
    """
    chosen = chosen_surface(worker_surface)
    if chosen != "cmux":
        return chosen
    from conductor.cmux_setup import find_executable, inspect, install, repair

    def fall_back(why: str) -> str:
        if worker_surface == "cmux":
            # Asked for by name: refusing beats silently ignoring the ask.
            print("\ncmux is the selected worker surface and is not "
                  "usable.\n", file=sys.stderr, flush=True)
            print(why, file=sys.stderr, flush=True)
            print("\nStart with --worker-surface terminal to use tmux "
                  "panes instead.", file=sys.stderr, flush=True)
            raise SystemExit(2)
        print(f"\ncmux is not usable, so workers will run in tmux panes "
              f"instead.\n\n{why}\n", file=sys.stderr, flush=True)
        application_log("conductor", "surface.fell_back",
                        "cmux is not usable; using the tmux surface",
                        severity="warning", reason=why[:300])
        return "terminal"

    if install_missing and find_executable() is None:
        installed, detail = await install()
        if not installed:
            return fall_back(detail)
        print("cmux installed", flush=True)

    # Launch BEFORE configuring, not after. cmux rewrites its own config
    # from a template as it starts, and the rewrite drops socketPassword
    # while keeping socketControlMode: password - so a password written
    # first is erased by the launch, and every later command fails with
    # "Password mode is enabled but no socket password is configured",
    # from an app that is running and looks healthy.
    binary = find_executable()
    password = ""
    try:
        # One job: get cmux running AND accepting us, however many rounds
        # of launch-and-reconfigure that takes. Doing it as separate steps
        # here lost the race with cmux's own startup rewrite.
        password = await asyncio.to_thread(repair, binary)
    except OSError as exc:
        application_log("conductor", "cmux.config_failed",
                        f"could not configure cmux socket access: {exc}",
                        severity="warning", exc_info=True)

    state = await inspect(password=password or None)
    if state.usable:
        print(f"surface: {state.explain()}", flush=True)
        return "cmux"
    return fall_back(state.explain())


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(DEFAULT_HOME),
                        help="conductor state home (default: ~/.voice-conductor)")
    parser.add_argument("--root", action="append", default=[],
                        dest="search_roots", metavar="DIR",
                        help="directory to search for projects (repeatable)")
    parser.add_argument("--register", metavar="PATH",
                        help="register this project path before starting "
                             "(e.g. the current repo)")
    parser.add_argument("--worker-location", choices=("local", "cloud"),
                        help="where a worker runs unless a task says "
                             "otherwise: local (a tmux pane here) or cloud "
                             "(a Claude Code cloud session, readable at "
                             "claude.ai/code). Per task, just say so: "
                             "\"run this one in the cloud\".")
    parser.add_argument("--no-install-deps", action="store_true",
                        help="do not install a missing cmux; report it "
                             "instead")
    parser.add_argument("--boss", choices=("visible", "headless"),
                        default="visible",
                        help="where the Boss runs: visible (a session in "
                             "the worker surface you can open and type "
                             "into - the default) or headless (the "
                             "invisible SDK call)")
    parser.add_argument("--boss-transport", choices=("http", "stdio"),
                        default="stdio",
                        help="how the visible Boss reaches its tools: http "
                             "(the conductor's own loopback MCP endpoint) "
                             "or stdio (boss-mcp, the packaged helper)")
    parser.add_argument("--boss-ui", choices=("window", "none"),
                        default="window",
                        help="the Boss window: window (the voice-agent "
                             "app - spoken turns drawn live, typing goes "
                             "to the same Boss - the default) or none")
    parser.add_argument("--boss-window", choices=("bridge", "http"),
                        default="bridge",
                        help="how the Boss window is fed: bridge (the "
                             "page loaded into the native window, stdio "
                             "underneath, no port - the default) or http "
                             "(a loopback page the window points at)")
    parser.add_argument("--new-chat", action="store_true",
                        help="start a new voice conversation: the first "
                             "thing you say opens a fresh Boss session "
                             "instead of continuing the last one")
    parser.add_argument("--worker-surface", choices=("cmux", "terminal"),
                        help="where managed workers are visible: terminal "
                             "(a tmux pane, drawn inside the Boss window - "
                             "the default with --boss-ui window) or cmux "
                             "(one workspace per agent, in the cmux app - "
                             "the default without the window)")
    parser.add_argument("--quiet", action="store_true",
                        help="do not speak notifications aloud; toast and "
                             "bell only")
    parser.add_argument("--reply", choices=("speak", "text", "both"),
                        default=None)
    parser.add_argument("--no-hotkey", action="store_true",
                        help="start listening immediately instead of Fn")
    parser.add_argument("--debug", action="store_true",
                        help="verbose console logging (the file log is "
                             "always complete)")
    parser.add_argument("--takeover", action="store_true",
                        help="if a conductor is already running in this "
                             "home, ask it to exit and take its place. "
                             "Without this a second conductor refuses to "
                             "start, because two of them share one "
                             "microphone, one keyboard and one set of "
                             "tmux session names.")
    args = parser.parse_args()

    home = Path(args.home).expanduser()
    # Before anything can fail: the log file exists and its path is on
    # screen, so a crashed startup is still debuggable after the fact.
    log_path = configure_logging(home, debug=args.debug)
    install_asyncio_exception_handler(asyncio.get_running_loop())
    stall_monitor = start_loop_stall_monitor("conductor")
    print(f"debug log: {log_path}\nrun id: {current_run()}", flush=True)
    application_log("conductor", "app.started", "voice conductor starting",
                    home=str(home), reply=args.reply or boss.REPLY_MODE,
                    quiet=args.quiet, no_hotkey=args.no_hotkey)

    # One conductor per home, before anything is started that a second
    # one would fight over. Two of them share the microphone (so the Boss
    # and a worker are spoken at once - VoiceCoordinator serialises
    # within a process and cannot serialise across two), the tmux session
    # names (so every message to a worker bounces with `duplicate
    # session`), and the workers themselves.
    try:
        await asyncio.to_thread(instance.acquire, home, current_run(),
                                takeover=args.takeover, argv=list(sys.argv))
    except instance.AlreadyRunning as clash:
        print(f"{clash}\n"
              "Quit that one first, or start again with --takeover.",
              file=sys.stderr, flush=True)
        return 1
    held = True

    load_env()
    api_key = (os.environ.get("OPENAI_API_KEY", "")
               or await asyncio.to_thread(ask_for_api_key))
    withhold_api_key()
    if not api_key:
        print("OPENAI_API_KEY is empty - paste your key into .env",
              file=sys.stderr)
        application_log("conductor", "app.missing_api_key",
                        "OPENAI_API_KEY is empty", severity="error")
        instance.release(home, current_run())
        return 1

    # The Boss window draws every worker's terminal itself, so with the
    # window on the workers live in plain tmux sessions - cmux would be a
    # second app drawing the same sessions. Saying --worker-surface cmux
    # still means cmux.
    if args.worker_surface is None and args.boss_ui == "window":
        args.worker_surface = "terminal"

    search_roots = args.search_roots or [os.getcwd()]
    # Before anything else that can take a while: the permissions computer
    # use needs, asked for now rather than discovered by a worker whose
    # clicks vanish. macOS never asks on its own; the conductor opens the
    # pane for each grant this terminal lacks, once, and says which apps
    # to add. Nothing at all when everything is granted.
    await asyncio.to_thread(
        ask_for_missing_grants, home,
        worker_app="cmux" if chosen_surface(args.worker_surface) == "cmux"
        else None)
    surface = await preflight_surface(args.worker_surface,
                                      install_missing=not args.no_install_deps)
    conductor = build_conductor(home, search_roots,
                                args.worker_location, surface,
                                boss_mode=args.boss,
                                boss_transport=args.boss_transport,
                                embedded_terminals=args.boss_ui == "window"
                                and surface != "cmux")
    from conductor import turn_toast
    from conductor.jump import JumpServer

    # The Boss window, once open, IS where the sessions are: a
    # notification's deep link opens the session's embedded terminal in
    # it - never cmux, which the window replaced (assigned below, after
    # the window opens).
    boss_page, boss_window = None, None

    async def focus_session(task_id: str) -> None:
        """Where a notification's or toast's deep link lands."""
        if boss_page is not None and surface != "cmux":
            boss_page.show_terminal(task_id)
            if boss_window is not None:
                await asyncio.to_thread(raise_boss_window, boss_window.pid)
            return
        await conductor.focus_task(task_id)

    jump = JumpServer(home, focus_session)
    # task_id -> what a toast would say about it, kept current from the
    # same cards the overlay draws.
    toast_rows: dict[str, dict] = {}
    bridge = None
    if isinstance(conductor.manager, PtyManagerBackend):
        from conductor.claude_manager import _manager_tools, _serialize
        if args.boss_transport == "http":
            # The conductor answers MCP itself, on the loopback interface;
            # the URL is the Boss session's business, not the user's. It
            # offers exactly this conductor's manager tools - the same 22
            # the SDK Boss had - and nothing else.
            bridge = ConductorMcp(conductor, _serialize, home,
                                  names=_manager_tools(conductor))
        else:
            bridge = BossBridge(home / "boss" / "tools.sock", conductor, _serialize)
        await bridge.start()
        conductor.manager.attach_bridge(bridge)
        # Where a toast's link lands: focusing the session it names.
        try:
            await jump.start()
        except OSError:
            application_log("ui", "jump.unavailable",
                            "no loopback port for toast links; toasts will "
                            "name sessions without linking them",
                            severity="warning", exc_info=True)
        if args.new_chat:
            conductor.new_conversation()
        current = conductor.current_boss_session_id
        print(f"boss: visible session ({current or 'opening now'})",
              flush=True)
        # Opening the Boss takes ~30 s, and it used to happen on the first
        # thing the user said after a restart. Start it now, in the
        # background; a turn that arrives first waits on the same lock.
        warm = getattr(conductor.manager, "warm", None)
        if warm is not None:
            asyncio.create_task(warm(conductor))
    if args.register:
        project = conductor.locator.register(args.register)
        print(f"registered: {project.display_name} ({project.id})")
        application_log("conductor", "project.registered",
                        project.display_name, project_id=project.id,
                        path=project.root_path)
    report = await conductor.startup()
    known = conductor.projects.list()
    print(f"home: {home}  |  {len(known)} project(s) registered")
    application_log("conductor", "app.startup_complete",
                    f"{len(known)} project(s) registered",
                    projects=len(known),
                    recovered_tasks=report["recovered_tasks"],
                    missing_projects=report["missing_projects"])
    if report["recovered_tasks"]:
        print(f"reconnected {len(report['recovered_tasks'])} running task(s)")
    if report["missing_projects"]:
        print(f"missing project paths: {report['missing_projects']}")
    waiting = [t for t in conductor.list_tasks()
               if t.status in ("waiting_for_user", "interrupted")]
    if waiting:
        print(f"{len(waiting)} task(s) waiting for you: "
              + ", ".join(t.title for t in waiting))

    # The live session reads its instructions from boss; point it at the
    # conductor-front prompt for this process only.
    boss.FRONTEND_INSTRUCTIONS = _load_prompt(
        "voice_conductor.md",
        "You are the voice of a coding-task manager. The client runs "
        "everything the user says itself; never answer a request yourself "
        "- say something brief while it works and relay its answers "
        "conversationally.")

    local_uv = Path.home() / ".local/bin/uv"
    uv = str(local_uv) if local_uv.exists() else (shutil.which("uv") or "uv")
    spawn = [uv, "run", "--python-preference", "only-managed",
             "--python", "3.13"]
    # stdout carries the overlay's NDJSON protocol, so only stderr is piped
    # here: its warnings and tracebacks used to land nowhere at all.
    overlay = await asyncio.create_subprocess_exec(
        *spawn, str(HERE / "overlay.py"), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    ui = Ui(overlay)
    stderr_readers = [asyncio.create_task(
        drain_subprocess_stderr(overlay.stderr, "overlay"))]

    # One canonical notification object fans out to the persistent on-screen
    # notice stack and the status-bar bell. A notice collapses to a line or
    # two, expands via its chevron, and stays until dismissed - dismissal
    # marks it read; it never silently disappears.
    # Voice is the primary escalation path: the user should not have to
    # notice a card for the system to function. All spoken notifications
    # route through ONE VoiceCoordinator: one configured voice, one speech
    # stream, priority ordering, and user barge-in - notifications can
    # never talk over each other or answer in a second voice.
    # The speech path is the live session itself, so notifications are
    # spoken in the same voice, on the same audio stream, as everything else
    # the agent says. Two engines would be two voices able to overlap.
    voice = VoiceCoordinator(voice=boss.LIVE_VOICE)

    def announce(text: str, kind: str = "info", subject: str = "") -> None:
        if not args.quiet:
            voice.enqueue(text, kind, subject=subject)

    def spoken_line(notification: TaskNotification) -> str:
        who = (f"The {notification.project_name} "
               f"{notification.task_title} agent")
        if notification.type == "needs_input":
            return f"{who} needs you: {notification.body}"
        if notification.type == "completed":
            return f"{who} finished. {notification.body.splitlines()[0]}"
        if notification.type == "failed":
            return f"{who} failed: {notification.body}"
        return f"{who}: {notification.title}"

    # The policy inside the service decides what interrupts: telemetry
    # updates the Activity Center only; on_notify fires solely for
    # attention-worthy items (approval, input, completion, failure).
    # ------------------------------------------------------------------
    # The live card is a projection of canonical state and nothing else.
    # NotificationService keeps history and the bell; the Boss and the
    # voice get their own branch through the SupervisorInbox. None of the
    # three can make the card wrong or stale for the others.
    inbox = SupervisorInbox(home / "supervisor_inbox.jsonl")
    conductor.inbox = inbox
    speech = SpeechPolicy(mode=getattr(boss, "NOTIFY_MODE", "important"))
    said: dict[str, tuple] = {}          # spoken text -> event ids
    speak_timer = {"handle": None}

    def project_state(event) -> None:
        """subagent.state_changed -> card. Deterministic, model-free."""
        if event.type in ("approval.resolved", "task.approval_resolved") \
                and event.task_id:
            # Answered - by the policy, the user, or the worker's own
            # window - before the voice got to it. A routine approval the
            # policy waves through must not reach the Boss as a question.
            by = (event.data or {}).get("resolved_by", "")
            if inbox.withdraw(event.task_id, ("approval_required",),
                              f"resolved by {by or 'someone'}"):
                voice.drop_subject(event.task_id)
            return
        if event.type != "subagent.state_changed":
            return
        state = event.data.get("state") or {}
        if not state.get("task_id"):
            return
        card = project_card(state)
        ui.send(notice=card)
        remember_for_toast(card)
        application_log("ui", "session_card.projected",
                        f"{card['title']}: {card['status_text']}",
                        severity="debug", task_id=state["task_id"],
                        card_state=card["state"],
                        source_event=event.data.get("transition", {}).get("event_id", ""))
        offer_supervisory(event, state)

    # How many sessions a toast may choose between. Older ones fall off:
    # the Boss does not talk about a worker from this morning, and the
    # hook reads this file on every turn it takes.
    TOAST_KEEP = 40

    def remember_for_toast(card: dict) -> None:
        """Leave the hook what it needs to link this session."""
        if not jump.port:
            return
        task_id = card["task_id"]
        toast_rows.pop(task_id, None)           # newest last
        toast_rows[task_id] = {"task_id": task_id, "title": card["title"],
                               "status": card["status_text"],
                               "glyph": card["glyph"],
                               "url": jump.url_for(task_id)}
        while len(toast_rows) > TOAST_KEEP:
            toast_rows.pop(next(iter(toast_rows)))
        try:
            turn_toast.write_sessions(home, list(toast_rows.values()))
        except OSError:
            application_log("ui", "toast.sessions_unwritable",
                            "the turn toast will not know about "
                            f"{task_id}", severity="debug", exc_info=True)

    def offer_supervisory(event, state: dict) -> None:
        """The semantic branch: only what deserves the Boss's attention."""
        transition = event.data.get("transition") or {}
        if transition.get("outcome") not in ("applied", "lifecycle"):
            return
        status, kind = state.get("status"), transition.get("kind")
        result = state.get("result") or {}
        sup_type, summary = None, ""
        if status == "waiting_for_approval" and "pending_approval" in transition.get("changed", []):
            sup_type = "approval_required"
            summary = (state.get("pending_approval") or {}).get("question", "")
        elif status == "waiting_for_input" and "pending_input" in transition.get("changed", []):
            sup_type = "input_required"
            summary = (state.get("pending_input") or {}).get("question", "")
        elif kind == "completed" and "result" in transition.get("changed", []):
            sup_type, summary = "completed", result.get("summary", "")
        elif status == "failed" and transition.get("from") != "failed":
            sup_type, summary = "failed", result.get("summary", "")
        elif status == "interrupted" and transition.get("from") in ("working", "starting"):
            sup_type, summary = "unexpected_interruption", "the worker stopped"
        if sup_type is None:
            return
        _, task = conductor._find_task(state["task_id"])
        project = conductor.projects.get(task.project_id)
        supervisory = SupervisoryEvent(
            event_id=event_id_for(transition.get("event_id", ""), sup_type),
            task_id=task.id, subagent_id=f"sub_{task.id}", type=sup_type,
            # Whole. A question is short by construction; a finish is the
            # worker's final message, and it is the Boss's to read - a
            # 300 cut here left "Fix: PR #110" behind (23:41:59Z).
            summary=summary,
            requires_action=sup_type in ("approval_required", "input_required"),
            trace_id=event.trace_id, source_event_id=event.event_id,
            task_title=task.title,
            project_name=project.display_name if project else "")
        if inbox.offer(supervisory):
            application_log("manager", "supervisor.event_offered",
                            f"{sup_type} for {task.title}", severity="debug",
                            task_id=task.id, supervisory_id=supervisory.event_id)
            record = getattr(conductor.manager, "record_supervisory", None)
            if record is not None:
                record(supervisory)
            # And into the Boss's own session, as it happens, when the
            # worker is one the Boss created.
            deliver = getattr(conductor.manager, "deliver_supervisory", None)
            if deliver is not None and deliver(supervisory):
                application_log("manager", "supervisor.event_pushed",
                                f"{sup_type} for {task.title} typed into the Boss",
                                severity="debug", task_id=task.id)
            schedule_speech()

    def schedule_speech() -> None:
        """Decide what to say a beat later, so finishes that land together
        become one sentence."""
        loop = asyncio.get_running_loop()
        if speak_timer["handle"] is not None:
            speak_timer["handle"].cancel()
        speak_timer["handle"] = loop.call_later(SPEECH_BEAT_S, speak_pending)

    def speak_pending() -> None:
        speak_timer["handle"] = None
        if args.quiet:
            for pending in inbox.pending_for_voice():
                inbox.record_voice(pending.event_id, "suppressed_policy", "--quiet")
            return
        for utterance in speech.decide(inbox, voice.covered_subjects()):
            said[utterance.text] = utterance.event_ids
            announce(utterance.text, utterance.kind, subject=utterance.subject)
            application_log("voice", "voice.decision",
                            utterance.text[:120], severity="debug",
                            decision="queued", event_ids=list(utterance.event_ids))

    def spoken(text: str) -> None:
        for event_id in said.pop(text, ()):
            inbox.record_voice(event_id, "spoken")

    def yielded(text: str) -> None:
        for event_id in said.get(text, ()):
            inbox.record_voice(event_id, "interrupted_by_user")

    def boss_speaks(event) -> None:
        """The Boss judged a worker update worth hearing (tell_user).

        Workers report to the Boss; the mechanical announcer above is
        limited to what blocks the user. Everything else the user hears
        about a worker is said here, in the Boss's words, once."""
        if event.type != "boss.tell_user":
            return
        text = plain_text(event.data.get("text", ""))
        if text:
            announce(text, "boss", subject="boss")

    voice.on_spoken = spoken
    voice.on_yielded = yielded
    conductor.bus.subscribe(boss_speaks)
    conductor.bus.subscribe(project_state)

    def on_activity(notification: TaskNotification) -> None:
        """History and the bell follow the notification. The card does
        not: it follows canonical state, above."""

    def on_notify(notification: TaskNotification) -> None:
        ui.send(bell=notifications.snapshot())
        inbox.record_notification_for(notification.task_id)

    def on_supersede(notification: TaskNotification) -> None:
        """The same finish, newer words. If the earlier line is still
        waiting to be said, the newer supervisory event supersedes it on
        the same subject; nothing is added, only replaced."""
        if voice.has_pending(notification.task_id):
            schedule_speech()

    def on_resolve(notification_id: str) -> None:
        # The underlying state resolved (e.g. approval answered directly in
        # the session): the on-screen row forgets its dismissal and waits
        # for the projection to rewrite it in place, and anything still
        # queued to say about it is dropped rather than spoken about a
        # question that no longer exists.
        ui.send(notice_remove=notification_id)
        # Drop by task, which is what the spoken line was queued under; the
        # notification id would never match it.
        resolved = next((n for n in notifications.list()
                         if n.id == notification_id), None)
        voice.drop_subject(resolved.task_id if resolved else notification_id)
        ui.send(bell=notifications.snapshot())

    notifications = NotificationService(conductor, on_notify=on_notify,
                                        on_supersede=on_supersede,
                                        on_activity=on_activity,
                                        on_resolve=on_resolve)
    # Telemetry mutates Activity Center rows in place without popping:
    # refresh the bell on progress, throttled so a chatty worker cannot
    # flood the pipe.
    last_bell = {"at": 0.0}

    def refresh_bell(event) -> None:
        import time as _time
        if event.type in ("runtime.progress", "task.context_updated",
                          "task.paused", "task.resumed", "task.interrupted",
                          "task.cancelled"):
            now = _time.monotonic()
            if now - last_bell["at"] > 2.0:
                last_bell["at"] = now
                ui.send(bell=notifications.snapshot())

    conductor.bus.subscribe(refresh_bell)
    ui.send(bell=notifications.snapshot())   # restart: rebuild from store
    # The tray renders from canonical state, not from replayed
    # notifications: every visible worker comes back showing what it is
    # actually doing now, and nothing that already finished is announced
    # again.
    for state in conductor.subagent_states():
        ui.send(notice=project_card(state))

    async def read_overlay() -> None:
        """Dropdown clicks: mark the task (or one notification) read."""
        while True:
            line = await overlay.stdout.readline()
            if not line:
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                application_log("overlay", "overlay.invalid_message",
                                "overlay sent a line that is not JSON",
                                severity="warning",
                                line=line.decode(errors="replace")[:300])
                continue
            if event.get("event") == "notice_dismissed":
                # Dismissing the on-screen card is the read receipt. Cards
                # are keyed on the task now, so the whole session's history
                # is marked read - mark_read expects a notification id and
                # would have matched nothing.
                notifications.store.mark_task_read(event.get("id", ""))
                ui.send(bell=notifications.snapshot())
                # And in canonical state, so the card stays dismissed
                # across every later tick and across a restart, until a
                # result or a question arrives after this point.
                dismiss = getattr(conductor, "dismiss_card", None)
                if dismiss is not None:
                    dismiss(event.get("id", ""))
            if event.get("event") == "notice_opened":
                # Body click: back to the exact working session that
                # produced this update (waking/reopening it if needed).
                notifications.store.mark_task_read(
                    event.get("task_id") or event.get("id", ""))
                ui.send(bell=notifications.snapshot())
                if event.get("task_id"):
                    async def open_task(task_id=event["task_id"]):
                        try:
                            await focus_session(task_id)
                        except Exception as exc:
                            print(f"focus failed: {exc}", file=sys.stderr,
                                  flush=True)
                            application_log(
                                "ui", "surface.focus_failed",
                                f"could not focus {task_id}",
                                severity="error", exc_info=True,
                                task_id=task_id, source="notice_opened")
                    asyncio.create_task(open_task())
            if event.get("event") == "notification":
                if event.get("notification_id"):
                    notifications.store.mark_read(event["notification_id"])
                elif event.get("task_id"):
                    notifications.store.mark_task_read(event["task_id"])
                ui.send(bell=notifications.snapshot())
                # A notification is a deep link: focus the task's session
                # surface, recovering it if the window was closed.
                if event.get("task_id"):
                    async def focus(task_id=event["task_id"]):
                        try:
                            await focus_session(task_id)
                        except Exception as exc:
                            print(f"focus failed: {exc}", file=sys.stderr,
                                  flush=True)
                            application_log(
                                "ui", "surface.focus_failed",
                                f"could not focus {task_id}",
                                severity="error", exc_info=True,
                                task_id=task_id, source="notification")
                    asyncio.create_task(focus())

    overlay_reader = asyncio.create_task(read_overlay())

    async def watchdog() -> None:
        """Nothing stays stuck silently.

        A worker held at a boot dialog waits for a keypress nobody is there
        to give, and a task whose PTY died stays "running" for ever. Both
        are invisible until you go looking, so sweep on a timer rather than
        waiting for someone to notice.
        """
        while True:
            await asyncio.sleep(boss.SWEEP_INTERVAL_S)
            try:
                report = await conductor.sweep_stuck()
            except Exception:
                application_log("conductor", "watchdog.sweep_failed",
                                "stuck-session sweep failed",
                                severity="error", exc_info=True)
                continue
            if report["workers_gone"]:
                ui.send(bell=notifications.snapshot())

    # Every sweep republishes it; publish once now so a computer-use
    # worker started in the first half-minute already holds its lease.
    await asyncio.to_thread(conductor.publish_gui_lease)
    sweeper = asyncio.create_task(watchdog())

    agent = ConductorVoice(api_key, ui, conductor,
                           args.reply or boss.REPLY_MODE)

    # The Boss window: the voice-agent app over this same conductor.
    # Spoken turns are mirrored onto its page as they happen; what is
    # typed into it goes to the same Boss the voice talks to. Failing to
    # open it never stops the voice - the window is a view, not the Boss.
    if args.boss_ui == "window":
        from conductor.app_web import (BossApp, CodexWeb,
                                         _focus_with_cmux, _tmux_task_ids)

        def plain_chat():
            """New chat: an ordinary coding session in this checkout,
            not another Boss. The Boss is the one that runs workers;
            a chat is a chat."""
            from conductor.claude_app import ClaudeApp
            return ClaudeApp(cwd=str(HERE), on_approval=lambda a: None)

        boss_page = CodexWeb(
            BossApp(conductor), home=home,
            focus=_focus_with_cmux if surface == "cmux" else None,
            plain=plain_chat)
        if surface != "cmux":
            # The workers live in plain tmux sessions here; asking cmux
            # after every one would only launch or wake an app nothing
            # is shown in.
            boss_page.list_cmux = _tmux_task_ids
        try:
            window_bridge = None
            if args.boss_window == "bridge":
                # The page straight into the native window, updates over
                # the spawned process's stdio: no HTTP, no port
                # (2026-09-01, "stay native").
                from conductor.app_web import WindowBridge
                await boss_page.open()
                window_bridge = WindowBridge(boss_page)
                await window_bridge.start(
                    uv, HERE / "conductor" / "app_mac.py", home / "boss")
                boss_window = window_bridge.process
            else:
                await boss_page.start()
                boss_window = await asyncio.create_subprocess_exec(
                    uv, "run", "--script",
                    str(HERE / "conductor" / "app_mac.py"),
                    "--url", boss_page.url + "?app=1",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE)
            stderr_readers.append(asyncio.create_task(
                drain_subprocess_stderr(boss_window.stderr, "boss-ui")))
            agent.mirror = boss_page
            # The window opens behind whatever the user is doing and
            # comes forward on the voice turn that asks for the Boss
            # (SHOW_AFTER_TURNS) - the window, not a cmux workspace.
            def raise_by_pid() -> None:
                """System Events fronts across apps where a background
                app's own activate is denied - measured 2026-09-01: the
                bridge raise alone left the user's Terminal in front."""
                try:
                    raise_boss_window(boss_window.pid)
                except Exception:
                    pass

            def show_boss_window() -> None:
                window_bridge.raise_window()
                asyncio.get_event_loop().run_in_executor(None, raise_by_pid)

            if isinstance(conductor.manager, PtyManagerBackend):
                conductor.manager.on_prose = boss_page.mirror_delta
                if window_bridge is not None:
                    conductor.manager.show_window = show_boss_window
                else:
                    pid = boss_window.pid
                    conductor.manager.show_window = \
                        lambda: raise_boss_window(pid)
            if window_bridge is not None:
                # A toast or notification link now opens the session in
                # THIS window; cmux stays where it is (asked 2026-09-01:
                # "we dont want cmux to pop up anymore").
                async def open_in_window(task_id: str) -> None:
                    row = toast_rows.get(task_id) or {}
                    window_bridge.open_task(task_id,
                                            str(row.get("title") or ""))
                    await asyncio.to_thread(raise_by_pid)
                jump.focus = open_in_window
                print("boss window: native bridge (no port)", flush=True)
            else:
                print(f"boss window: {boss_page.url}", flush=True)
        except Exception:
            application_log("ui", "boss_ui.unavailable",
                            "the Boss window could not open; the voice "
                            "runs without it", severity="warning",
                            exc_info=True)
            if boss_page is not None:
                await boss_page.stop()
            if isinstance(conductor.manager, PtyManagerBackend):
                conductor.manager.on_prose = None
            boss_page, boss_window = None, None

    def boss_interim(text: str) -> None:
        """The Boss's first sentence of a slow turn, said while it works."""
        text = plain_text(text)
        agent.boss_interim = text
        announce(text, "boss", subject="boss")
    if hasattr(conductor.manager, "on_interim"):
        conductor.manager.on_interim = boss_interim
    conductor.bus.subscribe(notes_between_turns(agent))
    # Close the loop: the coordinator's one speech path is this session.
    voice.set_speaker(agent.announce)
    session_task = asyncio.create_task(agent.run())

    def on_key(down: bool) -> None:
        # User barge-in: the moment they hold to talk, any system speech
        # yields; it resumes queued items after.
        if down:
            voice.interrupt_for_user()
        else:
            voice.user_finished()

    hotkey = None
    if not args.no_hotkey:
        # Read off the loop, so a busy Manager cannot delay the key: the
        # gate flips on the hotkey thread, the loop only gets told.
        hotkey = HotkeyListener(spawn, agent, on_change=on_key)
        hotkey.start(asyncio.get_running_loop())

    # SIGTERM is how one conductor asks another to leave (--takeover), and
    # Python's default handler ends the process where it stands: no
    # shutdown, no released lock, no reaped Boss - so the worker still
    # driving Chrome would keep its lease until the file went stale.
    # Cancelling the wait instead runs the same shutdown ctrl-c runs.
    #
    # SIGTERM only. SIGHUP looked like it belonged here too and does not:
    # the conductor is started detached, from a worker's shell, with
    # `nohup ./conduct.sh &`, and nohup works by setting SIGHUP to
    # SIG_IGN. add_signal_handler REPLACES that - so handling SIGHUP
    # would undo the one thing keeping the conductor alive when the
    # window it was launched from closes, and the user would find it
    # gone with a clean log. Hanging up is not a request to shut down.
    waiting = asyncio.current_task()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM,
                                lambda: (application_log(
                                    "conductor", "app.signalled",
                                    "shutting down on SIGTERM"),
                                    waiting.cancel()))
    except (AttributeError, NotImplementedError, RuntimeError):
        pass              # a platform without it; ctrl-c still works
    try:
        if args.no_hotkey:
            agent.holding = True
            await session_task
        else:
            print("hold Fn and talk, release to get an answer, "
                  "double-tap Fn for notifications, ctrl-c to quit",
                  flush=True)
            await hotkey.wait_with(session_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        application_log("conductor", "app.interrupted",
                        "shutting down on interrupt")
    except Exception:
        application_log("conductor", "app.crashed",
                        "the conductor exited with an unhandled error",
                        severity="error", exc_info=True)
        raise
    finally:
        agent.holding = False
        await agent.close()
        session_task.cancel()
        overlay_reader.cancel()
        sweeper.cancel()
        for reader in stderr_readers:
            reader.cancel()
        stall_monitor.cancel()
        notifications.close()
        if hasattr(conductor.manager, "close"):
            await conductor.manager.close()
        if bridge is not None:
            await bridge.stop()
        if boss_page is not None:
            await boss_page.stop()
        if boss_window is not None and boss_window.returncode is None:
            boss_window.terminate()
        if surface != "cmux":
            # In the app's own window there is no cmux for the Boss to
            # live on in: a cond_boss left running is a headless claude
            # (and its boss-mcp) nobody can see. The conversation itself
            # is on disk and resumes next launch.
            try:
                reap = await asyncio.create_subprocess_exec(
                    "tmux", "kill-session", "-t", "=cond_boss",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                await reap.wait()
            except OSError:
                application_log("conductor", "boss.reap_failed",
                                "the Boss tmux session could not be "
                                "closed", severity="warning",
                                exc_info=True)
        await jump.stop()
        ui.send(state="quit")
        if hotkey is not None:
            hotkey.stop()
        if overlay.returncode is None:
            overlay.terminate()
        # Last, and unconditionally: the lock is also the GUI lease, and a
        # worker still holding the screen must stop the moment there is no
        # conductor behind it. Letting go here is what turns "the
        # conductor quit" into "the worker's next click is refused".
        if held:
            instance.release(home, current_run())
        application_log("conductor", "app.stopped", "voice conductor stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
