"""The Boss as a Codex CLI session: what differs from Claude Code.

The Boss is hosted the same way whichever CLI it is (pty_manager.py):
a process in its own window, its tools served by boss-mcp over the
bridge socket, its replies read off its own transcript. This module is
only the part Codex answers differently. Measured on codex-cli 0.151.0,
2026-09-11, in a private tmux server:

  - Tools. boss-mcp is named with `-c mcp_servers.boss.command=...`;
    there is no --mcp-config file. Its calls are refused - "MCP tool
    call requires approval, but approval policy is never" - until the
    server says `default_tools_approval_mode = "approve"`.
  - The credential. Everything on a `-c` flag is on the command line,
    where `ps` shows it. So the command is a launcher script in the
    Boss's directory, mode 0700, that holds the token and execs the
    helper: the same place Claude Code's mcp.json (0600) keeps it.
  - What the Boss must not have. Claude Code takes --strict-mcp-config
    and --disallowedTools. Codex has neither. The user's own MCP servers
    (and the ones plugins bring - computer use among them) are switched
    off one by one, by name, from Codex's own effective listing; the
    shell, sub-agents, plugins, apps and browser/computer use are
    feature flags, switched off only when this Codex knows the flag
    (an unknown one is a hard error: "Unknown feature flag"); and the
    sandbox is read-only with no approvals, so nothing it could still
    reach writes anything.
  - A quoted key is not a key. `-c 'mcp_servers."computer-use".enabled
    =false'` made a NEW server called "computer-use" with its quotes
    ("invalid transport"); the bare `mcp_servers.computer-use` works. A
    name bare TOML cannot spell is left alone, and said so.
  - Identity. There is no --session-id. Codex writes no rollout until
    the first message (measured: nothing on disk after 10 s at the
    prompt; the file appeared within a second of the first message),
    so a fresh Boss is adopted at its prompt and its id is read off the
    rollout that first message creates (TmuxClaudeRuntime,
    adopt_at_prompt). A known id resumes with `codex resume <id>`.
  - Dialogs. "Update available!" (turned off here) and "Do you trust
    the contents of this directory?" - which `-c projects.<dir>` does
    not pre-answer; the runtime answers it, as it does for a worker.
  - notify. The user's config runs a program on every turn end (the
    computer-use client, here). It is theirs, for their sessions; the
    Boss runs with none.

Stop-hook toasts have no Codex equivalent (a notify program's output
is shown nowhere), so a Codex Boss's turns carry no toast.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from .boss_tools import SERVER_NAME
from .observability import application_log

# The launcher boss-mcp is started through. Also what names this Boss's
# process on the process table: it is on codex's command line, and no
# other process on the machine has it.
LAUNCHER = "boss-mcp-codex"
INSTRUCTIONS = "AGENTS.md"
# It routes work rather than doing it, so speed beats depth - the same
# call as the Claude Boss's low effort. The model is the user's own
# Codex default.
EFFORT = "low"
MCP_STARTUP_S = 90
# Hands a Boss must not have, as Codex names them. Only those this
# Codex knows are passed.
DISABLED_FEATURES = ("shell_tool", "unified_exec", "multi_agent",
                     "multi_agent_v2", "plugins", "apps", "computer_use",
                     "browser_use", "browser_use_external", "in_app_browser",
                     "image_generation", "goals", "memories")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml(text: str) -> str:
    """A TOML basic string. JSON's escapes are TOML's for anything a
    path or an effort name contains."""
    return json.dumps(text)


def known_features(binary: str) -> set[str] | None:
    """The feature names this Codex accepts, or None if it cannot say."""
    try:
        done = subprocess.run([binary, "features", "list"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return {line.split()[0] for line in done.stdout.splitlines()
            if line.strip()}


def other_servers(binary: str, features: list[str]) -> list[str] | None:
    """Every enabled MCP server this Codex would start that is not ours:
    the user's config and whatever its plugins add, as Codex itself
    lists them with the Boss's feature flags applied. None if it cannot
    say."""
    argv = [binary]
    for feature in features:
        argv += ["--disable", feature]
    try:
        done = subprocess.run([*argv, "mcp", "list", "--json"],
                              capture_output=True, text=True, timeout=20)
        rows = json.loads(done.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return [str(row.get("name")) for row in rows
            if isinstance(row, dict) and row.get("enabled", True)
            and row.get("name") and row.get("name") != SERVER_NAME]


def write_launcher(boss_dir: Path, helper: Path, socket: Path,
                   names: tuple[str, ...], boss_id: str,
                   credential: str) -> Path:
    """The command Codex starts boss-mcp with. The credential lives in
    here, readable by the user alone, and never on a command line."""
    path = boss_dir / LAUNCHER
    path.write_text(
        "#!/bin/sh\n"
        "# boss-mcp for the Codex Boss. Generated by the Voice Conductor\n"
        "# for one Boss session; do not edit.\n"
        f"BOSS_MCP_TOKEN={shlex.quote(credential)}\n"
        f"BOSS_SESSION_ID={shlex.quote(boss_id)}\n"
        "export BOSS_MCP_TOKEN BOSS_SESSION_ID\n"
        f"exec {shlex.quote(str(helper))} --socket {shlex.quote(str(socket))} "
        f"--tools {shlex.quote(','.join(names))} \"$@\"\n")
    path.chmod(0o700)
    return path


def argv(binary: str, launcher: Path, resume: str | None = None,
         features: set[str] | None = None,
         others: list[str] | None = None,
         effort: str | None = EFFORT) -> list[str]:
    """The Codex Boss's command line: fresh, or `codex resume <id>`.

    features: what this Codex knows (known_features); None passes none
    rather than risk an unknown one stopping the launch. others: the
    servers to switch off (other_servers)."""
    out = [binary]
    if resume:
        out.append("resume")
    out += ["-c", "check_for_update_on_startup=false",
            "-c", "notify=[]",
            "-c", f"mcp_servers.{SERVER_NAME}.command={_toml(str(launcher))}",
            "-c", f"mcp_servers.{SERVER_NAME}.startup_timeout_sec={MCP_STARTUP_S}",
            "-c", f"mcp_servers.{SERVER_NAME}.default_tools_approval_mode="
                  f"{_toml('approve')}"]
    for name in others or ():
        if not _BARE_KEY.match(name):
            application_log("manager", "boss.codex_server_left_on",
                            f"cannot switch off MCP server {name!r} from the "
                            "command line; the Codex Boss may see its tools",
                            severity="warning")
            continue
        out += ["-c", f"mcp_servers.{name}.enabled=false"]
    for feature in DISABLED_FEATURES:
        if features is not None and feature in features:
            out += ["--disable", feature]
    out += ["-c", f"web_search={_toml('disabled')}",
            "-c", "include_apply_patch_tool=false"]
    if effort:
        out += ["-c", f"model_reasoning_effort={_toml(effort)}"]
    out += ["-s", "read-only", "-a", "never"]
    if resume:
        out.append(resume)
    return out


def resumable(adapter, boss_dir: Path, session_id: str) -> bool:
    """A Codex session this Boss can pick up: its rollout exists and was
    written in this directory. A Claude Code id (a conversation the
    Claude Boss had) is not one."""
    path = adapter.transcript_for(str(boss_dir), session_id)
    if path is None or not path.exists():
        return False
    from .codex_adapter import rollout_cwd
    cwd = rollout_cwd(path)
    return bool(cwd) and os.path.realpath(cwd) == os.path.realpath(boss_dir)


def launch_argv(binary: str, launcher: Path, resume: str | None) -> list[str]:
    """argv, with this machine's Codex asked what it knows first."""
    features = known_features(binary)
    wanted = [f for f in DISABLED_FEATURES
              if features is not None and f in features]
    others = other_servers(binary, wanted)
    if others is None:
        application_log("manager", "boss.codex_servers_unknown",
                        "could not list Codex's MCP servers; the Boss is "
                        "started without switching any off",
                        severity="warning")
    return argv(binary, launcher, resume=resume, features=features,
                others=others)
