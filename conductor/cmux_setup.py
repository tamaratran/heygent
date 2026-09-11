"""cmux as a declared dependency, not a lucky accident of $PATH.

cmux is the visible surface Managed Subagents run in, so "is it here and
can we drive it" is a startup question with a real answer, checked before
anything is launched.

On bundling: cmux is GPLv3. Redistribution is permitted, but shipping it
inside a combined work carries copyleft obligations that are a human's
decision, not a build step - and this product bundles nothing anyway. It
is a repository run with uv: no installer, no .app, nothing to embed a
terminal emulator into. So installation is first-run setup and detection
is automatic afterwards.

Discovery is deterministic and does not trust $PATH. The Homebrew cask
links /usr/local/bin/cmux as a symlink INTO the app bundle, so the bundle
is the real location and the link is a convenience that a stale shell, a
different user, or a launchd context may not have.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Where a real install puts it, most authoritative first. The bundle path
# is canonical: /usr/local/bin/cmux is a symlink into it.
KNOWN_PATHS = (
    Path("/Applications/cmux.app/Contents/Resources/bin/cmux"),
    Path.home() / "Applications/cmux.app/Contents/Resources/bin/cmux",
    Path("/opt/homebrew/bin/cmux"),
    Path("/usr/local/bin/cmux"),
)

SOCKET = Path.home() / ".local/state/cmux/cmux.sock"

# The version this was proved against. Not the version that introduced the
# API - nobody has told us that - so it is the honest floor rather than a
# guess at the real one.
MIN_VERSION = (0, 64, 0)
VERIFIED_VERSION = "0.64.22"

# What a Managed Subagent surface actually needs. Anything absent means the
# surface cannot be driven, whatever else works.
REQUIRED = ("ping", "new-workspace", "list-workspaces", "select-workspace",
            "focus-pane", "send", "list-pane-surfaces")

INSTALL_HELP = """cmux is required for visible worker sessions and was not found.

    brew tap manaflow-ai/cmux
    brew install --cask cmux

or download the DMG:
    https://github.com/manaflow-ai/cmux/releases/latest

Then start cmux once - its control socket only exists while it is running -
and allow this app to reach it by setting, in ~/.config/cmux/cmux.json:

    "automation": {
      "socketControlMode": "password",
      "socketPassword": "<a secret>"
    }

followed by `cmux reload-config`. Put the same secret in
~/.config/cmux/.voice-agent-socket-password (mode 0600) or the
CMUX_SOCKET_PASSWORD environment variable."""


@dataclass
class CmuxDependency:
    installed: bool = False
    version: str | None = None
    compatible: bool = False
    executable_path: str | None = None
    socket_available: bool = False
    capabilities_ok: bool = False
    missing: list[str] = field(default_factory=list)
    problem: str = ""

    @property
    def usable(self) -> bool:
        """Every part has to hold. Installed but unreachable is not usable,
        and reporting it as such is how a caller ends up sending input into
        nothing."""
        return (self.installed and self.compatible
                and self.socket_available and self.capabilities_ok)

    def as_dict(self) -> dict:
        return {"installed": self.installed, "version": self.version,
                "compatible": self.compatible,
                "executablePath": self.executable_path,
                "socketAvailable": self.socket_available,
                "capabilitiesOk": self.capabilities_ok,
                "missing": list(self.missing), "problem": self.problem}

    def explain(self) -> str:
        """One line a user can act on, or the setup instructions."""
        if self.usable:
            return f"cmux {self.version} ready ({self.executable_path})"
        if not self.installed:
            return INSTALL_HELP
        if not self.compatible:
            return (f"cmux {self.version} is too old; this build needs "
                    f"{'.'.join(map(str, MIN_VERSION))} or newer "
                    f"(verified against {VERIFIED_VERSION}). "
                    "Update with: brew upgrade --cask cmux")
        if not self.socket_available:
            return ("cmux is installed but its control socket is not "
                    "answering. Start cmux - the socket exists only while "
                    "the app runs - and check socketControlMode.\n\n"
                    + (self.problem or ""))
        return (f"cmux is reachable but missing required commands: "
                f"{', '.join(self.missing)}. Update with: "
                "brew upgrade --cask cmux")


# How cmux says it cannot be driven at all, as opposed to one command
# failing. Measured by killing cmux while workers were running: the app
# leaves its socket FILE behind, so a dead cmux answers "Failed to connect
# to socket ... (Connection refused, errno 61)" - NOT "Socket not found",
# which was the only string the callers recognised. A dead app was
# therefore reported as a failed command, and the caller retried instead
# of saying cmux is gone.
_GONE = ("socket not found", "connection refused", "failed to connect")
_DENIED = "access denied"
# cmux REWRITES ~/.config/cmux/cmux.json from a template every time it
# launches, and the rewrite drops socketPassword while keeping
# socketControlMode: password. The app is then running, reachable, and
# refusing every command - which is neither "gone" nor "denied", and is
# repairable by writing the password back.
_UNCONFIGURED = "no socket password is configured"
# cmux with no window at all. It drops its window when the last workspace
# closes, and then cannot make a workspace ("TabManager not available")
# until it has one again. Measured: every workspace closed to start
# clean, and the Boss could not be opened for the next ten minutes -
# every message died with "never returned to its prompt".
_NO_WINDOW = "tabmanager not available"


def needs_window(text: str) -> bool:
    """Whether this failure is cmux having no window to put anything in -
    fixable with new-window, then a retry."""
    return _NO_WINDOW in text.lower()


def needs_repair(text: str) -> bool:
    """Whether this failure is one we can fix and retry, rather than
    report. Both causes are cmux's own doing: it was closed, or it
    rewrote its config and lost the password we configured."""
    lowered = text.lower()
    return (_UNCONFIGURED in lowered
            or any(mark in lowered for mark in _GONE))


def unavailable_reason(text: str) -> str | None:
    """Why cmux cannot be driven, or None if this was an ordinary failure."""
    lowered = text.lower()
    if _UNCONFIGURED in lowered:
        return "cmux lost its socket password (it rewrites its own config)"
    if any(mark in lowered for mark in _GONE):
        return "cmux is not running"
    if _DENIED in lowered:
        return ('cmux denied access; set automation.socketControlMode '
                'to "password"')
    return None


BREW_TAP = "manaflow-ai/cmux"
BREW_CASK = "cmux"
CMUX_CONFIG = Path.home() / ".config" / "cmux" / "cmux.json"


def _readable_json(text: str) -> dict:
    """cmux writes its config as JSON with comments.

    Whole-line // comments are all it uses, and stripping those is enough
    to read the settings back. Anything unparseable is treated as an empty
    config rather than an error - the caller backs the file up before
    writing, so the worst case is a template we replace, not settings we
    silently lose.
    """
    try:
        return json.loads(text)
    except ValueError:
        pass
    kept = [line for line in text.splitlines()
            if not line.lstrip().startswith("//")]
    try:
        return json.loads("\n".join(kept))
    except ValueError:
        return {}


def ensure_socket_access(config_path: Path | None = None,
                         password_file: Path | None = None) -> str:
    """Give ourselves a way in, and remember it. Returns the password.

    cmux only accepts socket control when it is configured to, and the
    setting lives in the user's own cmux config - so a working install is
    still an unusable one until this runs. Every part of it was a real
    failure: a machine with cmux installed and socketControlMode unset
    refuses every command; and cmux REWRITES this file from a template on
    some launches, which dropped the password mid-session and stopped the
    app booting at all. Running this at startup makes that self-healing.

    The password is also written where the runtime looks for it, so
    nothing depends on the user exporting an environment variable.
    """
    config_path = config_path or CMUX_CONFIG
    from .cmux_client import PASSWORD_FILE
    password_file = password_file or PASSWORD_FILE

    config: dict = {}
    if config_path.exists():
        config = _readable_json(config_path.read_text())
    automation = config.setdefault("automation", {})
    password = str(automation.get("socketPassword") or "").strip()
    if not password:
        # A remembered password is preferred over a new one: cmux may have
        # dropped it from a rewritten config while workers still hold it.
        try:
            password = password_file.read_text().strip()
        except OSError:
            password = ""
    if not password:
        password = secrets.token_urlsafe(24)

    wanted = {"socketControlMode": "password", "socketPassword": password}
    if any(automation.get(k) != v for k, v in wanted.items()):
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            # One copy, overwritten. This runs on every repair round, and
            # a timestamped backup per round buries the user's own config
            # in clutter.
            backup = config_path.with_name(config_path.name + ".voice-agent.bak")
            backup.write_text(config_path.read_text())
        automation.update(wanted)
        config_path.write_text(json.dumps(config, indent=2) + "\n")

    password_file.parent.mkdir(parents=True, exist_ok=True)
    password_file.write_text(password)
    password_file.chmod(0o600)
    return password


SIDEBAR_SOURCE = Path(__file__).parent / "sidebars" / "conductor.swift"
SIDEBAR_DEST = Path.home() / ".config/cmux/sidebars/conductor.swift"


def install_sidebar(source: Path = SIDEBAR_SOURCE,
                    dest: Path = SIDEBAR_DEST) -> bool:
    """Put the delegation sidebar where cmux reads custom sidebars.

    The file shows the Boss and, under it, a clickable bar per session it
    delegated to, with a loading indicator until the worker reports back.
    Copying only on change keeps cmux's hot-reload quiet on ordinary
    launches; SHOWING the sidebar stays the user's choice (right-click the
    sidebar button and pick "conductor"), so nothing of theirs is
    overridden. Returns whether the file was (re)written.
    """
    try:
        text = source.read_text()
        if dest.exists() and dest.read_text() == text:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        return True
    except OSError:
        from .observability import application_log
        application_log("runtime", "cmux.sidebar_install_failed",
                        "could not install the conductor sidebar",
                        severity="warning", exc_info=True)
        return False


def find_executable() -> str | None:
    """The cmux binary, by known location before $PATH.

    An env var wins, for packaging and for tests; then the app bundle,
    which is where the Homebrew symlink points anyway; then $PATH last,
    because a shell that happens to have it is not a dependency contract.
    """
    override = os.environ.get("CMUX_BINARY")
    if override and Path(override).exists():
        return override
    for path in KNOWN_PATHS:
        if path.exists():
            return str(path)
    found = shutil.which("cmux")
    return found


def parse_version(text: str) -> tuple | None:
    """cmux prints "cmux 0.64.22 (102) [ddd4a01bc]"."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in match.groups()) if match else None


async def _run(*args: str, timeout: float = 900.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        return 1, str(exc)
    return proc.returncode, out.decode(errors="replace").strip()


async def install(announce=print) -> tuple[bool, str]:
    """Install cmux, rather than telling the user to.

    cmux is the default surface, so on a machine without it the product
    does not start - printing brew commands and exiting is a setup step we
    were leaving to the user. Homebrew only: a cask is something the user
    can inspect, update and remove by the usual means, which downloading a
    DMG ourselves would not be. Without brew we still explain.
    """
    # Both Homebrew prefixes: Apple Silicon puts it in /opt/homebrew and
    # Intel in /usr/local, and hardcoding one of them refused to install
    # on a machine that had brew in the other.
    brew = shutil.which("brew")
    if brew is None:
        brew = next((path for path in ("/opt/homebrew/bin/brew",
                                       "/usr/local/bin/brew")
                     if Path(path).exists()), None)
    if brew is None:
        return False, ("Homebrew is not installed, so cmux cannot be "
                       "installed automatically.\n" + INSTALL_HELP)
    announce("installing cmux (one time, via Homebrew)...")
    code, text = await _run(brew, "tap", BREW_TAP, timeout=300.0)
    if code != 0 and "already tapped" not in text.lower():
        return False, f"brew tap {BREW_TAP} failed:\n{text[-500:]}"
    code, text = await _run(brew, "install", "--cask", BREW_CASK)
    if code != 0 and "already installed" not in text.lower():
        return False, f"brew install --cask {BREW_CASK} failed:\n{text[-500:]}"
    return True, "cmux installed"


# How cmux is launched when it is not running: in the background. The
# app is needed for its control socket - the Boss and every worker live
# in its workspaces - not for its window, and the conductor decides
# separately when a window is worth showing (a worker starting, the Boss
# once the conversation is one). Measured without this: cmux jumped in
# front at every launch, and quitting it just brought it straight back,
# because the Boss lives inside it and was relaunched. -g: do not bring
# to the foreground; -j: launch hidden.
LAUNCH = ("open", "-g", "-j", "-a", "cmux")
# -j asks for a hidden launch, and cmux shows its window anyway once it
# has a workspace - measured: visible, behind the front app, after a
# -g -j launch. So a cmux WE launched is hidden explicitly once it is
# up. Hidden, it still makes workspaces, runs their commands and renders
# their screens (measured through the runtime's own create path: 6 s,
# the same as visible), and select-workspace + open -a bring it back
# when there is something to show.
HIDE = ("osascript", "-e",
        'tell application "System Events" to set visible of process "cmux" to false')
VISIBLE = ("osascript", "-e",
           'tell application "System Events" to get visible of process "cmux"')


def _osa(command: tuple) -> str:
    try:
        done = subprocess.run(list(command), capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (getattr(done, "stdout", "") or "").strip()


def hide_app(for_s: float = 4.0, every_s: float = 0.1) -> None:
    """Take a cmux we launched off the screen, and keep it there while it
    finishes launching.

    One hide is not enough: cmux shows its window as it comes up, and a
    hide issued before that is undone by it - measured, hidden at 0.6 s,
    visible from 0.5 s on. So for a few seconds after the launch, every
    time it is seen visible it is hidden again; the window is on screen
    for a fraction of a second rather than the two it took to answer a
    ping. Best effort: a cmux that stays visible is a nuisance, not a
    failure.
    """
    deadline = time.monotonic() + for_s
    while time.monotonic() < deadline:
        if _osa(VISIBLE) == "true":
            _osa(HIDE)
        time.sleep(every_s)


async def ensure_running(binary: str, password: str,
                         attempts: int = 20) -> bool:
    """Start cmux if it is not up. Its control socket only exists while
    the app runs, so an installed cmux is still an unreachable one."""
    env = dict(os.environ, CMUX_QUIET="1", CMUX_SOCKET_PASSWORD=password)

    async def ping() -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "ping", stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, env=env)
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
        except (OSError, asyncio.TimeoutError):
            return False
        return proc.returncode == 0 and b"PONG" in out

    if await ping():
        return True
    await _run(*LAUNCH, timeout=30.0)
    # Hiding runs alongside the wait for the socket: the window shows
    # well before cmux answers, and the hide has to be there when it does.
    hiding = asyncio.ensure_future(asyncio.to_thread(hide_app))
    try:
        for _ in range(attempts):
            await asyncio.sleep(1.0)
            if await ping():
                return True
        return False
    finally:
        await hiding


def repair(binary: str | None = None, timeout: float = 30.0) -> str:
    """Get cmux back to a state we can drive, and return the password.

    Called when a command has already failed. Launching is safe to repeat
    (open on a running app just activates it) and rewriting the password
    is safe to repeat (a remembered one is restored, not replaced), so
    this can run on any failure without deciding first WHICH of the two
    went wrong.

    Order matters and cost us an evening: cmux rewrites its config as it
    launches, so a password written before the launch is erased by it.
    Write it after.
    """
    binary = binary or find_executable()
    if binary is None:
        return ""
    password = ""
    launched = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running():
            subprocess.run(list(LAUNCH), capture_output=True, timeout=30)
            launched = True
            hide_app(for_s=2.0)          # in place of the old sleep(2.0)
            continue
        # Written EVERY round, not once. A launching cmux rewrites this
        # file from its template after we have written to it, so a single
        # write loses the race with its own startup - measured: the app
        # came up and then refused every command for want of the password
        # we had just configured.
        password = ensure_socket_access()
        if _ping_sync(binary, password):
            if launched:
                hide_app(for_s=1.0)      # the window can still be on its way
            return password
        time.sleep(1.0)
    return password


def is_running() -> bool:
    found = subprocess.run(["pgrep", "-x", "cmux"], capture_output=True)
    return found.returncode == 0


def _ping_sync(binary: str, password: str) -> bool:
    env = dict(os.environ, CMUX_QUIET="1", CMUX_SOCKET_PASSWORD=password)
    try:
        done = subprocess.run([binary, "ping"], capture_output=True,
                              text=True, env=env, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and "PONG" in done.stdout


async def inspect(binary: str | None = None,
                  password: str | None = None) -> CmuxDependency:
    """The whole startup question, answered once.

    resolve -> version -> ping -> capabilities, stopping at the first
    thing that fails so the message names the actual problem rather than
    the last symptom of it.
    """
    state = CmuxDependency()
    path = binary or find_executable()
    if path is None:
        return state
    state.installed = True
    state.executable_path = path

    env = dict(os.environ)
    env["CMUX_QUIET"] = "1"
    secret = password or _stored_password()
    if secret:
        env["CMUX_SOCKET_PASSWORD"] = secret

    async def run(*args: str) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                path, *args, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env)
            out, err = await asyncio.wait_for(proc.communicate(), 15)
        except (OSError, asyncio.TimeoutError) as exc:
            return 1, str(exc)
        return proc.returncode, (out.decode() + err.decode()).strip()

    code, text = await run("--version")
    version = parse_version(text)
    if version is None:
        state.problem = f"could not read a version from {text[:120]!r}"
        return state
    state.version = ".".join(str(part) for part in version)
    state.compatible = version >= MIN_VERSION
    if not state.compatible:
        return state

    code, text = await run("ping")
    state.socket_available = code == 0 and "PONG" in text
    if not state.socket_available:
        state.problem = text[:200]
        return state

    code, text = await run("--help")
    state.missing = [name for name in REQUIRED if name not in text]
    state.capabilities_ok = not state.missing
    return state


def _stored_password() -> str:
    from .cmux_client import PASSWORD_FILE
    try:
        return PASSWORD_FILE.read_text().strip()
    except OSError:
        return ""


def main() -> int:
    """`python3 -m conductor.cmux_setup` - the check CI and dev bootstrap
    run. Exit code is the answer, so a release cannot ship a build that
    expects cmux and cannot drive it."""
    import sys
    state = asyncio.run(inspect())
    print(json.dumps(state.as_dict(), indent=2))
    print()
    print(state.explain())
    return 0 if state.usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
