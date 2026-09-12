"""Is Claude Code signed in? Asked before the Boss is started on it.

The Boss and every worker are `claude` sessions. Without a login, each of
them opens on Claude Code's sign-in screen, inside a tmux window the user
may never see - the voice hears them, says it is passing it on, and no
answer ever comes. `claude auth status` knows, so the conductor asks it
first and says what to do.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys

from . import dialogs
from .observability import application_log

# Either of these is a login of its own: the CLI takes it over any stored
# account, and `auth status` does not always know about it.
TOKEN_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

LOGIN_COMMAND = "claude auth login"
NOT_SIGNED_IN = (
    "heygent runs its assistant on Claude Code, and Claude Code is not "
    "signed in on this Mac.\n\n"
    f"Sign in with your Claude account, then open heygent again:\n\n"
    f"    {LOGIN_COMMAND}\n\n"
    "(A Claude Pro/Max subscription or an Anthropic API key with credit is "
    "needed.)")


def is_logged_in(env=None, run=subprocess.run, which=shutil.which) -> bool | None:
    """True or False from `claude auth status`; None when it could not
    say (no claude on PATH, an older CLI, unreadable output) - then the
    Boss's own window is where a missing login shows up, as before."""
    env = os.environ if env is None else env
    if any(env.get(var) for var in TOKEN_VARS):
        return True
    claude = which("claude")
    if not claude:
        return None
    try:
        done = run([claude, "auth", "status", "--json"],
                   capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired):
        application_log("conductor", "claude.auth_status_failed",
                        "claude auth status could not be run",
                        severity="warning", exc_info=True)
        return None
    try:
        status = json.loads(done.stdout or "{}")
    except ValueError:
        application_log("conductor", "claude.auth_status_unreadable",
                        "claude auth status printed no JSON",
                        severity="warning",
                        exit=done.returncode, stderr=done.stderr[-300:])
        return None
    logged_in = status.get("loggedIn")
    if not isinstance(logged_in, bool):
        return None
    application_log("conductor", "claude.auth_status",
                    f"claude auth: {'signed in' if logged_in else 'not signed in'}",
                    method=status.get("authMethod", ""))
    return logged_in


def open_login_terminal(run=subprocess.run, which=shutil.which) -> bool:
    """Terminal, with the login already typed in: the sign-in is Claude
    Code's own interactive flow (a browser page and a code to paste), so
    a shell is where it has to happen. The CLI is named by the path this
    process found it at: a fresh install is in ~/.local/bin, which the
    user's own shell may not have on its PATH yet."""
    claude = which("claude") or "claude"
    command = f"{shlex.quote(claude)} auth login"
    script = ('on run argv\n'
              '  tell application "Terminal"\n'
              '    activate\n'
              '    do script (item 1 of argv)\n'
              '  end tell\n'
              'end run')
    try:
        done = run(["osascript", "-", command], input=script,
                   capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        application_log("conductor", "claude.login_terminal_failed",
                        "could not open Terminal for the Claude login",
                        severity="warning", exc_info=True)
        return False
    return done.returncode == 0


def ensure_logged_in(env=None, *, alert=dialogs.alert,
                     open_terminal=open_login_terminal,
                     status=is_logged_in) -> bool:
    """True when the Boss can be started. Otherwise the user has been
    told - and, from Finder, offered a Terminal with the login in it."""
    if status(env) is not False:
        return True
    application_log("conductor", "claude.not_logged_in", NOT_SIGNED_IN,
                    severity="error")
    if dialogs.has_terminal():
        dialogs.tell(NOT_SIGNED_IN)
        return False
    print(NOT_SIGNED_IN, file=sys.stderr, flush=True)
    if alert(NOT_SIGNED_IN, ("Quit", "Sign in")) == "Sign in":
        open_terminal()
    return False
