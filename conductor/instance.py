"""One conductor at a time, and one keyboard between them.

Two conductors on one machine is not a degraded mode, it is a broken one.
They would share the microphone - VoiceCoordinator serialises speech
within a process and cannot serialise two, because a second session is a
second audio stream by construction - the tmux session names, and the
workers themselves. Nothing prevented it; on 2026-09-08 it was believed
to have happened and there was no way to tell from inside, which is
itself the problem. (It had not: `ps` lists `uv run ... conduct.py` beside
the python it spawned, and one conductor reads as two. The bounced
messages that night had a different cause - see
tmux_runtime._claim_name.)

So the second one cannot start. This module is that rule:

    lock = acquire(home, run_id)          # or AlreadyRunning
    ...
    heartbeat(home, run_id, gui_tasks=[...])
    release(home, run_id)

and the same file is the GUI lease. A worker allowed to drive the screen
asks `lease(...)` before every action that moves the cursor or presses a
key: it may act only while the conductor that supervises it is the live
one and still counts it as a running computer-use task. A worker whose
conductor died, or that a restart did not re-adopt, therefore stops
typing on its own, rather than being a hand on the keyboard nobody owns -
which is what one of them was, still driving Chrome, on the same night.

Liveness is pid PLUS start time. A pid alone is recycled - and the pid of
a conductor that died in the night is exactly the kind of number a fresh
process gets - so the lock records what `ps` says the process started at,
and a pid whose start time has changed is somebody else.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import dialogs

DEFAULT_HOME = Path.home() / ".voice-conductor"
LOCK_NAME = "conductor.lock"

# The env a worker is launched with, so a worker's own invocation of the
# computer driver can name itself. Set in tmux_runtime.scrub_argv, read
# here and in conductor/computer.py.
TASK_ENV = "VOICE_CONDUCTOR_TASK_ID"
HOME_ENV = "VOICE_CONDUCTOR_HOME"

# How long a heartbeat may be stale before the lease stops believing it.
# The sweep refreshes it every SWEEP_INTERVAL_S (30 s); a conductor whose
# loop has been wedged for four sweeps is not supervising anything, and a
# worker of its should not be typing on the user's behalf.
LEASE_STALE_S = 150.0


class AlreadyRunning(RuntimeError):
    """Another conductor holds this home. Carries the instance it found."""

    def __init__(self, instance: "Instance", detail: str = "") -> None:
        self.instance = instance
        super().__init__(detail or (
            f"another conductor is already running here: pid "
            f"{instance.pid}, run {instance.run_id}, started "
            f"{instance.started_at or 'unknown'}"))


@dataclass(frozen=True)
class Instance:
    pid: int
    run_id: str = ""
    # What `ps` says this pid started at. Identity, not decoration: a
    # recycled pid has a different one.
    started_at: str = ""
    # Wall clock of the last heartbeat, monotonic-free so it survives a
    # reboot comparison honestly (an old file simply reads as stale).
    updated_at: float = 0.0
    # The tasks this conductor is supervising that are allowed to drive
    # the GUI. Anything not in here has no lease.
    gui_tasks: tuple[str, ...] = ()
    argv: tuple[str, ...] = ()
    home: str = ""

    def as_json(self) -> dict:
        return {"pid": self.pid, "run_id": self.run_id,
                "started_at": self.started_at, "updated_at": self.updated_at,
                "gui_tasks": list(self.gui_tasks), "argv": list(self.argv),
                "home": self.home}


def lock_path(home: str | Path | None = None) -> Path:
    return Path(home or DEFAULT_HOME).expanduser() / LOCK_NAME


def process_started_at(pid: int, run=subprocess.run) -> str:
    """What `ps` says this pid started at, or "" if it is not there.

    `ps -o lstart=` is stable for the life of a process and different for
    a pid that has been handed out again, which is the whole reason it is
    read. Anything unexpected from ps reads as "not there": refusing to
    start is recoverable, taking over a live conductor's home is not.
    """
    if pid <= 0:
        return ""
    try:
        done = run(["ps", "-p", str(pid), "-o", "lstart="],
                   capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return " ".join(done.stdout.split())


def alive(instance: Instance | None, run=subprocess.run) -> bool:
    """Is the process this lock names still the process that wrote it?"""
    if instance is None or instance.pid <= 0:
        return False
    started = process_started_at(instance.pid, run=run)
    if not started:
        return False
    if not instance.started_at:
        # Written by an older build with no start time. The pid is all
        # there is; believing it is the safer error - it refuses a second
        # conductor rather than allowing one.
        return True
    return started == instance.started_at


def read(home: str | Path | None = None) -> Instance | None:
    """The instance recorded here, whether or not it is still alive."""
    path = lock_path(home)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        pid = int(raw.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return Instance(pid=pid,
                    run_id=str(raw.get("run_id") or ""),
                    started_at=str(raw.get("started_at") or ""),
                    updated_at=float(raw.get("updated_at") or 0.0),
                    gui_tasks=tuple(str(t) for t in
                                    (raw.get("gui_tasks") or [])),
                    argv=tuple(str(a) for a in (raw.get("argv") or [])),
                    home=str(raw.get("home") or ""))


def _write(path: Path, instance: Instance) -> None:
    """Atomically, so a reader never sees half a lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(instance.as_json(), indent=2))
    temporary.replace(path)


def _log(event: str, message: str, **fields):
    # Imported lazily: instance.py is read by conductor/computer.py, which
    # a worker runs as a standalone script.
    from .observability import application_log
    application_log("conductor", event, message, **fields)


def acquire(home: str | Path | None, run_id: str, *, takeover: bool = False,
            argv: list[str] | None = None,
            wait_s: float = 15.0, sleep=time.sleep,
            run=subprocess.run) -> Instance:
    """Claim this home for this process, or say who already has it.

    takeover asks the live one to leave first (SIGTERM, then its own
    shutdown - which reaps its Boss session and closes its windows). It
    is deliberate and explicit: the default is to refuse, because the
    silent alternative is what left two conductors on one microphone.
    """
    path = lock_path(home)
    existing = read(home)
    if existing is not None and existing.pid != os.getpid() \
            and alive(existing, run=run):
        if not takeover:
            _log("conductor.instance_refused",
                 f"another conductor (pid {existing.pid}) is already "
                 f"running in {path.parent}; not starting a second one",
                 severity="error", data={"pid": existing.pid,
                                         "run_id": existing.run_id})
            raise AlreadyRunning(existing)
        _log("conductor.instance_takeover",
             f"asking the conductor at pid {existing.pid} to exit",
             severity="warning", data={"pid": existing.pid,
                                       "run_id": existing.run_id})
        try:
            os.kill(existing.pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if not alive(existing, run=run):
                break
            sleep(0.25)
        if alive(existing, run=run):
            raise AlreadyRunning(existing, (
                f"the conductor at pid {existing.pid} did not exit within "
                f"{wait_s:.0f}s; quit it yourself and start again"))
    elif existing is not None:
        _log("conductor.instance_stale_lock",
             f"clearing a lock left by pid {existing.pid} (run "
             f"{existing.run_id or 'unknown'}), which is not running",
             data={"pid": existing.pid, "run_id": existing.run_id})
    mine = Instance(pid=os.getpid(), run_id=run_id,
                    started_at=process_started_at(os.getpid(), run=run),
                    updated_at=time.time(),
                    argv=tuple(argv or []),
                    home=str(Path(home or DEFAULT_HOME).expanduser()))
    _write(path, mine)
    _log("conductor.instance_acquired",
         f"this conductor (pid {mine.pid}) holds {path.parent}",
         data={"run_id": run_id})
    return mine


ALREADY_RUNNING = ("heygent is already running on this Mac (pid {pid}, "
                   "started {when}). Two of them would share the "
                   "microphone, so this one is not starting.\n\nQuit the "
                   "other one - it may be a Terminal window - or take over: "
                   "that one closes and this one carries on.")


def claim(home: str | Path | None, run_id: str, *, takeover: bool = False,
          argv: list[str] | None = None, on_terminal: bool | None = None,
          alert=None, acquire=acquire) -> bool:
    """acquire, and when the home is taken say so where the user is: on
    the terminal when there is one, otherwise in a dialog that offers
    the takeover - a conductor opened from Finder has no --takeover to
    start again with, and the log is the only other place the refusal
    would go."""
    alert = dialogs.alert if alert is None else alert
    on_terminal = dialogs.has_terminal() if on_terminal is None else on_terminal
    try:
        acquire(home, run_id, takeover=takeover, argv=argv)
        return True
    except AlreadyRunning as clash:
        if on_terminal or takeover:
            print(f"{clash}\nQuit that one first, or start again with "
                  "--takeover.", file=sys.stderr, flush=True)
            return False
        existing = clash.instance
        print(clash, file=sys.stderr, flush=True)
        text = ALREADY_RUNNING.format(pid=existing.pid,
                                      when=existing.started_at or "earlier")
        if alert(text, ("Quit", "Take over")) != "Take over":
            return False
    try:
        acquire(home, run_id, takeover=True, argv=argv)
        return True
    except AlreadyRunning as clash:
        print(clash, file=sys.stderr, flush=True)
        alert(f"{clash}.", ("Quit",))
        return False


def heartbeat(home: str | Path | None, run_id: str,
              gui_tasks: list[str] | tuple[str, ...] = ()) -> bool:
    """Say we are still here, and which tasks may drive the screen.

    Returns False - and writes nothing - if the lock has moved on to
    another conductor. A process that lost the lock must not reclaim it
    by writing over it.
    """
    current = read(home)
    if current is not None and current.pid != os.getpid():
        return False
    mine = Instance(pid=os.getpid(), run_id=run_id,
                    started_at=(current.started_at if current is not None
                                and current.pid == os.getpid()
                                else process_started_at(os.getpid())),
                    updated_at=time.time(),
                    gui_tasks=tuple(dict.fromkeys(str(t) for t in gui_tasks)),
                    argv=current.argv if current is not None else (),
                    home=str(Path(home or DEFAULT_HOME).expanduser()))
    _write(lock_path(home), mine)
    return True


def release(home: str | Path | None, run_id: str) -> bool:
    """Give the home back, if it is still ours."""
    current = read(home)
    if current is None:
        return False
    if current.pid != os.getpid():
        return False
    try:
        lock_path(home).unlink()
    except OSError:
        return False
    _log("conductor.instance_released", "this conductor let go of its home",
         data={"run_id": run_id})
    return True


@dataclass(frozen=True)
class Lease:
    """Whether a worker may put its hands on the keyboard, and why not."""
    granted: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.granted


def lease(task_id: str | None, home: str | Path | None = None,
          environ: dict | None = None, now=time.time) -> Lease:
    """Is this caller a worker a live conductor is still supervising?

    Asked before every GUI action and answered to the worker as a warning,
    not a refusal: the driver acts either way, and a worker told it was
    left behind can tell the user.

    Always held when nobody is claiming to be a worker: the driver is also
    a command a person runs by hand, and the smoke test drives TextEdit
    with no conductor at all. The question is for a process that says "I
    am task X", which is what a worker's shell says through its
    environment.
    """
    environ = os.environ if environ is None else environ
    task_id = task_id or environ.get(TASK_ENV) or ""
    if not task_id:
        return Lease(True, "not a worker")
    home = home or environ.get(HOME_ENV) or DEFAULT_HOME
    current = read(home)
    if current is None:
        return Lease(False, (
            "no conductor is running here, so nothing is supervising this "
            f"worker ({task_id}). Tell the user the conductor is gone."))
    if not alive(current):
        return Lease(False, (
            f"the conductor that started this worker (pid {current.pid}) "
            f"is no longer running, so nothing is supervising {task_id}. "
            "Tell the user the conductor is gone."))
    age = now() - (current.updated_at or 0.0)
    if age > LEASE_STALE_S:
        return Lease(False, (
            f"the conductor (pid {current.pid}) has not checked in for "
            f"{age:.0f}s, so it is not supervising {task_id} while it is "
            "wedged."))
    if task_id not in current.gui_tasks:
        return Lease(False, (
            f"the running conductor (pid {current.pid}) does not count "
            f"{task_id} as a live computer-use task: a restart did not "
            "re-adopt this worker. Tell the user it was left behind."))
    return Lease(True, f"{task_id} is held by the conductor at pid "
                       f"{current.pid}")
