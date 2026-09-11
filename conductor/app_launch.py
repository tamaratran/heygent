"""What the app runs when it is opened: conduct.sh's job, without a shell.

The launcher (packaging/launcher.c) starts ``-m conductor.app_launch``
with whatever arguments the app was given. From Finder that is none, the
environment is launchd's - PATH=/usr/bin:/bin:/usr/sbin:/sbin, cwd / -
and nothing is reading stdout. So before handing over to conduct.py
this does what a terminal and conduct.sh used to do for it:

- PATH reaches the user's own tools first (claude, tmux, codex, git from
  ~/.local/bin and Homebrew), and the bundle's tmux and claude last, so
  an installed tmux keeps talking to the server it already runs;
- the Claude Code session markers and a stale ANTHROPIC_API_KEY are
  dropped, for the reasons conduct.sh gives;
- output goes to ~/.voice-conductor/logs/app-launch.log;
- the first run opens the setup window (conductor/app_setup_window.py)
  instead of asking on a terminal nobody is looking at, and the key it
  saved is handed to conduct.py the way a shell's environment would;
- a conductor that cannot start says so in an alert, rather than a
  Dock icon that bounces and vanishes;
- `--check` reports what the bundle carries and exits, which is how a
  build is tested without starting a voice session.
"""
from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

from conductor import app_bundle, app_setup, instance
from conductor.projects import DEFAULT_HOME

APP_ROOT = Path(__file__).resolve().parent.parent

# conduct.sh's list; see the comment there.
DROPPED_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_CHILD_SESSION",
               "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
               "CLAUDE_CODE_BRIDGE_SESSION_ID")
USER_TOOL_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
                  "/usr/local/bin")
SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def app_path(current: str, home: Path, bundled: Path | None) -> str:
    """User tools, then whatever PATH already had, then the system, then
    the bundle's own fallbacks - each directory once."""
    order = [str(home / d[2:]) if d.startswith("~/") else d
             for d in USER_TOOL_DIRS]
    order += [d for d in current.split(os.pathsep) if d]
    order += list(SYSTEM_DIRS)
    if bundled is not None:
        order.append(str(bundled))
    seen: list[str] = []
    for entry in order:
        if entry not in seen:
            seen.append(entry)
    return os.pathsep.join(seen)


def prepare_environment(env: dict, home: Path,
                        bundled: Path | None) -> dict:
    env = {k: v for k, v in env.items() if k not in DROPPED_ENV}
    env["PATH"] = app_path(env.get("PATH", ""), home, bundled)
    env.setdefault("HOME", str(home))
    # Finder gives no locale; tmux and the CLIs draw boxes in UTF-8.
    env.setdefault("LANG", "en_US.UTF-8")
    return env


def check() -> dict:
    """What this build carries, as JSON: the interpreter, every
    dependency imported for real, and the tools on the app's PATH."""
    import importlib
    import shutil
    report: dict = {
        "executable": sys.executable,
        "bundle": str(app_bundle.bundle_root() or ""),
        "python": sys.version.split()[0],
        "prefix": sys.prefix,
        "imports": {},
        "tools": {},
    }
    for module in ("aiohttp", "numpy", "sounddevice", "claude_agent_sdk",
                   "mcp", "Quartz", "ApplicationServices", "AVFoundation",
                   "AppKit", "WebKit", "boss", "voice_agent",
                   "conductor.conductor", "conductor.app_mac",
                   "conductor.computer"):
        try:
            importlib.import_module(module)
            report["imports"][module] = "ok"
        except BaseException as exc:  # a missing dylib is an OSError
            report["imports"][module] = f"{type(exc).__name__}: {exc}"
    for tool in ("tmux", "claude", "git"):
        report["tools"][tool] = shutil.which(tool) or ""
    report["ok"] = all(v == "ok" for v in report["imports"].values())
    return report


def log_path(home: Path) -> Path:
    return Path(home) / "logs" / "app-launch.log"


def log_output(home: Path) -> None:
    """From Finder nothing reads stdout; keep it where a person can."""
    if sys.stdout is not None and sys.stdout.isatty():
        return
    path = log_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a", buffering=1)
    os.dup2(handle.fileno(), 1)
    os.dup2(handle.fileno(), 2)


def conductor_home(argv: list[str]) -> Path:
    """conduct.py's --home, read the way its parser will read it."""
    for index, arg in enumerate(argv):
        if arg == "--home" and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser()
        if arg.startswith("--home="):
            return Path(arg.split("=", 1)[1]).expanduser()
    return DEFAULT_HOME


def show_alert(title: str, detail: str, run=subprocess.run) -> None:
    run([sys.executable, "-m", "conductor.app_setup_window",
         "--alert", title, "--detail", detail])


def log_tail(home: Path, lines: int = 6) -> str:
    try:
        text = log_path(home).read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(line for line in text.splitlines()[-lines:]
                     if line.strip())


def main(argv: list[str] | None = None, run=subprocess.run) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    home = Path.home()
    wanted = prepare_environment(dict(os.environ), home,
                                 app_bundle.bundled_bin())
    for key in set(os.environ) - set(wanted):
        del os.environ[key]
    os.environ.update(wanted)
    if "--check" in argv:
        report = check()
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    state_home = conductor_home(argv)
    log_output(state_home)
    if os.getcwd() == "/":
        os.chdir(home)
    name = app_bundle.APP_NAME

    running = instance.read(state_home)
    if instance.alive(running):
        show_alert(f"{name} is already running",
                   f"A conductor (pid {running.pid}) already manages "
                   f"{state_home}. Quit it first - two would share one "
                   "microphone and the same agents.", run=run)
        return 0

    forced = "--setup" in argv
    argv = [arg for arg in argv if arg != "--setup"]
    if forced or app_setup.needs_setup(state_home):
        done = run([sys.executable, "-m", "conductor.app_setup_window",
                    "--home", str(state_home)])
        if done.returncode != 0 or app_setup.needs_setup(state_home):
            return 0                       # closed the window: not now
    key = app_setup.read_key(state_home)
    if key and not os.environ.get(app_setup.KEY_NAME):
        # conduct.py takes it from here and withholds it from everything
        # it starts (voice_agent.withhold_api_key).
        os.environ[app_setup.KEY_NAME] = key

    sys.argv = [str(APP_ROOT / "conduct.py"), *argv]
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
        code = 0
    except SystemExit as exit:
        code = exit.code if isinstance(exit.code, int) else (
            0 if exit.code is None else 1)
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    if code:
        show_alert(f"{name} stopped",
                   (log_tail(state_home) or "It exited without saying why.")
                   + f"\n\nThe whole log: {log_path(state_home)}", run=run)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
