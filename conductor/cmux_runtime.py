"""CmuxClaudeRuntime: the same worker, hosted in a cmux workspace.

cmux cannot be a surface layered over the tmux runtime. CmuxSurface
attaches by resuming the provider session, and if a tmux pane is still
hosting that session the result is two processes on one conversation -
exactly the duplicate worker the whole design forbids. Whoever hosts the
PTY owns the execution, so choosing cmux is choosing a host, not a view.

Which turns out to be cheap. Every pane operation in TmuxClaudeRuntime
goes through one method and uses six verbs, so this subclass translates
those to cmux and inherits the rest unchanged: transcript discovery, turn
events, approval detection, the watchdog seams. The supervision is the
part worth keeping identical - a worker in cmux should be exactly as
observable as a worker in tmux, because it is the same claude writing the
same transcript.

    tmux                    cmux
    new-session       ->    workspace create, then send the command
    has-session       ->    workspace list, by title
    kill-session      ->    workspace close
    capture-pane      ->    read-screen
    send-keys -l      ->    send
    send-keys KEY     ->    send-key
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass

from pathlib import Path

from .cmux_setup import find_executable, needs_repair, needs_window, repair
from .observability import application_log
from .tmux_runtime import PROMPT_READY, TmuxClaudeRuntime, scrub_argv, \
    session_name


# What a worker must NOT inherit. A cmux workspace opens a login shell
# under the app, so it inherits whatever launched cmux - and if that was a
# Claude Code session, the child markers come with it. Claude Code sees
# CLAUDE_CODE_CHILD_SESSION and turns transcript saving OFF, which is the
# file every piece of our supervision reads: no turns, no completions, no
# approvals, and create_session times out waiting for a session file that
# is never going to be written. It reports it plainly on screen, which is
# the only reason this was findable.
#
# The list itself now lives in tmux_runtime, because the tmux path needs
# exactly the same protection and had none: one definition, both hosts.
# The prefix stays here for the login shell, which is a shell and not an
# argv, and is skipped when create_session already scrubbed the argv.
def scrub_prefix(name: str, home: str | None = None) -> str:
    """The `env -u ... VAR=... ` prefix for a line typed into a shell.

    A function rather than a constant, because the prefix now carries the
    worker's own task id (see tmux_runtime.scrub_argv): the workspace
    title is the session name, and the session name is the task.
    """
    task_id = name[len("cond_"):] if name.startswith("cond_") else ""
    return " ".join(shlex.quote(part) for part in
                    scrub_argv(task_id or None, home)) + " "


# Stamped on every workspace we create, and required before we will treat
# one as ours. Identity by title alone was not enough: a personal
# workspace the user happened to name cond_task_* would have been adopted,
# driven, and eventually closed under them.
MANAGED_MARK = "conductor-managed agent session"
# One colour for every managed workspace, so they read as a set in the
# sidebar. cmux has real workspace GROUPS, which would be better, but the
# socket API cannot create one: `--group` demands an existing group_id
# ("Error: invalid_params: Missing or invalid group_id") and
# workspace-action offers pin/rename/set-description/set-color and nothing
# about groups. Grouping stays a thing the user can do by hand.
MANAGED_COLOR = "Teal"


@dataclass
class _Result:
    """What the tmux seam returns, so callers cannot tell the difference."""
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


# The parent speaks tmux's key names; cmux has its own for the few that
# differ. Enter and Escape read the same in both.
_KEY_NAMES = {"BSpace": "backspace", "BTab": "shift+tab", "Space": "space",
              # C-u sets a person's draft aside (tmux_runtime.send); the
              # letters would have been typed otherwise.
              "C-u": "ctrl+u", "C-c": "ctrl+c", "Escape": "escape"}


class CmuxClaudeRuntime(TmuxClaudeRuntime):
    # Whether starting a worker raises cmux to the user. A class default
    # so it holds even for instances built without __init__.
    focus_on_create = True

    def __init__(self, *args, cmux_binary: str | None = None,
                 password: str | None = None,
                 focus_on_create: bool = True, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.focus_on_create = focus_on_create
        self.cmux = cmux_binary or find_executable()
        self._password = password
        # session name -> (workspace uuid, surface uuid). The names the
        # parent invents stay the keys, so its bookkeeping is untouched.
        self.places: dict[str, tuple[str, str]] = {}

    # -- talking to cmux -------------------------------------------------
    # Verbs after which a remembered workspace listing is wrong.
    _MUTATING = ("new-workspace", "close-workspace", "workspace-action",
                 "rename-workspace")
    # Verbs for which a cmux that is not answering is brought back. Every
    # one acts on the user's behalf: a turn typed to the Boss, a worker
    # started or closed, a window put in front. Listing and reading are
    # polling - the sweep lists workspaces every few seconds - and
    # repairing on those brought a cmux the user had just quit straight
    # back, every time, within seconds. Now it stays quit until their
    # next turn or the next worker, which need it and relaunch it hidden.
    _REPAIRING = ("new-workspace", "send", "send-key", "close-workspace",
                  "workspace-action", "rename-workspace", "select-workspace",
                  "focus-window", "new-window")

    def _cmux(self, *args: str, _repaired: bool = False,
              _windowed: bool = False) -> _Result:
        if not self.cmux:
            return _Result(1, "", "cmux is not installed")
        if args and args[0] in self._MUTATING:
            self._workspaces_cache = None
        env = dict(os.environ)
        env["CMUX_QUIET"] = "1"
        secret = self._password or _stored_password()
        if secret:
            env["CMUX_SOCKET_PASSWORD"] = secret
        try:
            done = subprocess.run([self.cmux, *args], capture_output=True,
                                  text=True, env=env, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _Result(1, "", str(exc))
        if done.returncode != 0 and not _windowed \
                and needs_window(done.stdout + done.stderr):
            # cmux is running with no window - the user closed the last
            # tab, or a clean-up did - and cannot make a workspace until
            # it has one. Give it one and try once more.
            self._cmux("new-window", _windowed=True)
            return self._cmux(*args, _repaired=_repaired, _windowed=True)
        if done.returncode != 0 and not _repaired \
                and args and args[0] in self._REPAIRING \
                and needs_repair(done.stdout + done.stderr):
            # cmux was closed, or lost its own password. Both are fixable
            # without the user restarting anything - when the user asks
            # for something that needs cmux (_REPAIRING). A listing that
            # fails because cmux is gone is answered as a failure, and
            # the caller marks what it cannot see as gone.
            #
            # Guarded like the run above, and for the same reason: this
            # method promises a _Result to every caller, and they read
            # .stdout off it without looking. repair() launches an app and
            # writes config files, so it can raise OSError or time out -
            # and a repair that fails is a repair that did not happen, not
            # a new class of error. Report the command's OWN failure, which
            # is what the caller asked about and what says why.
            try:
                self._password = repair(self.cmux) or self._password
            except (OSError, subprocess.TimeoutExpired) as exc:
                application_log("runtime", "cmux.repair_failed",
                                f"could not bring cmux back: {exc}",
                                severity="warning", exc_info=True,
                                command=" ".join(args))
                return _Result(done.returncode, done.stdout, done.stderr)
            return self._cmux(*args, _repaired=True)
        return _Result(done.returncode, done.stdout, done.stderr)

    def _rows(self, text: str) -> list[tuple[str, str, str]]:
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("cmux:"):
                continue
            parts = line.lstrip("* ").split()
            if len(parts) >= 2 and ":" in parts[0]:
                title = " ".join(parts[2:]).replace("[selected]", "").strip()
                out.append((parts[0], parts[1], title))
        return out

    # How long one workspace listing stays good for. _lookup lists on
    # EVERY call, including cache hits (to catch a cmux restart), and the
    # sweep looks up every task - so one sweep was twenty listings at
    # ~0.2s each. Within a second they all say the same thing. Anything
    # that changes the set of workspaces drops the cache (see _cmux).
    WORKSPACES_TTL_S = 1.0
    # How long a failed listing may be answered with the last good one.
    # Past this, cmux is not stumbling but gone - quit by the user, most
    # likely, taking its terminals with it - and "everyone is still here"
    # would keep every worker counted as live for ever. The watchers then
    # get their misses, and each asks the process table before it decides.
    LISTING_STALE_MAX_S = 30.0

    def _workspaces(self) -> list[dict]:
        """Every workspace, with the fields the text listing leaves out -
        description and working directory, which is how ours are told
        apart from the user's own."""
        cached = getattr(self, "_workspaces_cache", None)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self.WORKSPACES_TTL_S:
            return cached[1]
        out = self._cmux("workspace", "list", "--json")
        rows = None
        if out.returncode == 0:
            try:
                rows = json.loads(out.stdout).get("workspaces", [])
            except (ValueError, AttributeError):
                rows = None
        if rows is None:
            # cmux would not, or could not, answer. That is not "there are
            # no workspaces": answered that way, has-session says no for
            # every worker at once, and (measured 2026-08-29, 15:16:58Z
            # and 22:30:55Z) every watcher declared its session ended in
            # the same second while all of them were alive. Answer with
            # the last listing that was good - workspaces do not vanish
            # between one second and the next without us closing them -
            # and say so, once per streak, because a listing failure used
            # to leave no line at all.
            rows = list(getattr(self, "_last_good_listing", None) or [])
            since = getattr(self, "_listing_failing_since", None)
            if since is None:
                self._listing_failing_since = now
                application_log(
                    "runtime", "cmux.listing_failed",
                    "cmux did not list its workspaces; answering with "
                    f"the last good listing ({len(rows)} workspaces)",
                    severity="warning", returncode=out.returncode,
                    stderr=(out.stderr or "")[:300],
                    stdout=(out.stdout or "")[:300])
            elif rows and now - since >= self.LISTING_STALE_MAX_S:
                application_log(
                    "runtime", "cmux.listing_stale",
                    "cmux has not listed its workspaces for "
                    f"{now - since:.0f}s; no longer answering with the "
                    "last good listing", severity="warning",
                    stderr=(out.stderr or "")[:300])
                self._last_good_listing = []
                rows = []
        else:
            if getattr(self, "_listing_failing_since", None) is not None:
                application_log("runtime", "cmux.listing_recovered",
                                "cmux lists its workspaces again",
                                workspaces=len(rows))
            self._listing_failing_since = None
            self._last_good_listing = rows
        # Cached either way, for the TTL: a failure retried on every lookup
        # would turn one sweep into twenty slow calls.
        self._workspaces_cache = (now, rows)
        return rows

    def _managed_root(self) -> str:
        """Where this Conductor puts worker checkouts. A workspace running
        in there is ours even if it predates the marker."""
        directory = getattr(self.transcript, "dir", None)
        if not directory:
            return ""
        return str(Path(str(directory)).parent / "workspaces")

    def _is_ours(self, workspace: dict) -> bool:
        description = workspace.get("description") or ""
        if MANAGED_MARK in description:
            return True
        # Workspaces created before the marker existed carry no
        # description. Refusing those outright would orphan live workers
        # across an upgrade, and an orphaned worker is worse than a
        # loose rule: the parent would call the session gone and resume
        # it somewhere else, leaving two processes on one conversation.
        # Running inside our own workspaces directory is the evidence.
        root = self._managed_root()
        cwd = workspace.get("current_directory") or ""
        return bool(root and cwd.startswith(root))

    def _lookup(self, name: str) -> tuple[str, str] | None:
        """Find a worker's workspace by the title we gave it.

        Cached, because the parent asks constantly - but re-resolved when
        the cache misses, so a restart that lost the map can still find
        workers cmux is still showing.

        A cached entry is verified before it is trusted: if cmux restarted,
        every uuid we remember belongs to a workspace that no longer
        exists, and handing those back would send follow-ups into nowhere.
        """
        if name in self.places:
            workspace, _ = self.places[name]
            if any(w.get("id") == workspace for w in self._workspaces()):
                return self.places[name]
            self.places.pop(name, None)       # cmux restarted under us
        for workspace in self._workspaces():
            if workspace.get("custom_title") != name:
                continue
            if not self._is_ours(workspace):
                # Same name, not our work. Driving it would type into
                # somebody's own terminal.
                continue
            uuid = workspace.get("id", "")
            surfaces = self._rows(self._cmux(
                "list-pane-surfaces", "--workspace", uuid,
                "--id-format", "both").stdout)
            if surfaces:
                self.places[name] = (uuid, surfaces[0][1])
                return self.places[name]
        return None

    # -- the seam --------------------------------------------------------
    def _tmux(self, *args: str) -> _Result:
        """Every pane operation the parent performs, in cmux terms."""
        verb = args[0] if args else ""
        if verb == "new-session":
            return self._create(list(args))
        target = _target(list(args))
        if verb == "has-session":
            return _Result(0 if self._lookup(target) else 1)
        if verb == "list-sessions":
            # Names come from the JSON listing - the same source _lookup
            # trusts for send and focus - not the text one. The text
            # listing's column layout is cmux's to change, and it did:
            # 0.64 prints `workspace:12  cond_task_x` where the parser
            # expected `workspace:12 UUID  cond_task_x`, so every title
            # read as empty, the sweep found no worker alive, and each one
            # was declared gone thirty seconds after it started - while
            # has-session, asking by name, still said it was there.
            listed = self._cmux("workspace", "list", "--json")
            if listed.returncode != 0:
                # Do not answer "there are no workers" because cmux would
                # not talk to us. The caller marks everything missing from
                # this list as gone.
                return _Result(listed.returncode, "", listed.stderr
                               or "could not list cmux workspaces")
            try:
                workspaces = json.loads(listed.stdout).get("workspaces", [])
            except (ValueError, AttributeError):
                return _Result(1, "", "cmux workspace listing was not JSON")
            names = [w.get("custom_title") or "" for w in workspaces]
            return _Result(0, "\n".join(n for n in names if n))
        place = self._lookup(target) if target else None
        if place is None:
            return _Result(1, "", f"no cmux workspace for {target!r}")
        workspace, surface = place
        if verb == "kill-session":
            self.places.pop(target, None)
            return self._cmux("close-workspace", "--workspace", workspace)
        if verb == "capture-pane":
            lines = args[args.index("-S") + 1].lstrip("-") \
                if "-S" in args else "60"
            return self._cmux("read-screen", "--surface", surface,
                              "--scrollback", "--lines", lines)
        if verb == "send-keys":
            rest = [a for a in args[1:] if a not in ("-t", target)]
            if "-l" in rest:          # literal text
                text = rest[rest.index("-l") + 1]
                # cmux `send` presses Enter for a newline - a real one and
                # the two-character sequence alike - so a multi-line
                # message arrives as several submissions. Measured: the
                # Boss's context block was sent line by line and it
                # answered the first fragment. tmux's -l typed the text
                # verbatim; here the lines become one.
                # And a tab - a real one or the two-character `\t` - is
                # pressed as the TAB key, which moves focus in Claude
                # Code's input box and scrambles the characters typed
                # around it. A space stands in for it, same as a newline.
                text = " ".join(text.replace("\\n", " ").split("\n"))
                text = text.replace("\\t", " ").replace("\t", " ")
                return self._cmux("send", "--surface", surface, text)
            for key in rest:          # named keys, in tmux's spelling
                self._cmux("send-key", "--surface", surface,
                           _KEY_NAMES.get(key, key))
            return _Result(0)
        return _Result(1, "", f"unmapped operation {verb!r}")

    def _create(self, args: list[str]) -> _Result:
        """`tmux new-session -d -s NAME -c CWD cmd...` as a cmux workspace
        with the command typed into its terminal.

        The title is the session name so the parent's bookkeeping keeps
        working, and so a workspace can be found again after a restart -
        cmux has no notion of our session names otherwise.
        """
        name = args[args.index("-s") + 1]
        cwd = args[args.index("-c") + 1] if "-c" in args else os.getcwd()
        command = args[args.index("-c") + 2:] if "-c" in args else args[1:]
        line = self._launch_line(name, command) \
            if len(command) > 1 else (command[0] if command else "")
        if line and command[0] != "env":
            # create_session scrubs the argv itself now; only a caller that
            # built the command some other way still needs the prefix, and
            # a doubled `env -u ... env -u ...` is noise in the pane the
            # user is about to read.
            line = scrub_prefix(name, getattr(self, "home", None)) + line
        # Wake the pane. Measured: cmux does not bring up an unfocused
        # workspace's terminal until it is focused or handed input -
        # read-screen returned nothing for 33 s on a bare workspace - so
        # waiting for its prompt waited the whole timeout on every launch
        # (20 s, then 45 s), and only then typed. An empty creation-time
        # command wakes it in 0.1 s. The real command is NOT sent that
        # way: a creation-time command arrives before the login shell is
        # ready and is lost - measured, it printed above the "Last login"
        # banner and never ran, and the Boss sat at a bare prompt for
        # 150 s. It is typed below, once the prompt is stable, and
        # checked whole before Enter.
        described = ["--description", f"{MANAGED_MARK}: {name}"]
        made = self._cmux("new-workspace", "--name", name, "--cwd", cwd,
                          *described, "--command", "")
        if made.returncode != 0 and \
                "command" in (made.stdout + made.stderr).lower():
            # An older cmux without --command: the pane renders when it
            # renders; the prompt wait below is as patient as before.
            made = self._cmux("new-workspace", "--name", name, "--cwd", cwd,
                              *described)
        if made.returncode != 0:
            return made
        # cmux acknowledges new-workspace before its listing shows the
        # workspace. Measured under load: the listing lagged, the launch
        # was reported as "made no workspace", and the workspace appeared
        # a moment later - orphaned, with nothing typed into it. Ask a few
        # times before concluding it was not made.
        import time as _time
        place = None
        for _ in range(10):
            place = self._lookup(name)
            if place is not None:
                break
            _time.sleep(0.5)
        if place is None:
            return _Result(1, "", f"cmux made no workspace called {name!r}")
        workspace, surface = place
        # One colour for all of them: the closest cmux's API gets to
        # showing managed agents as a set (see MANAGED_COLOR).
        self._cmux("workspace-action", "--action", "set-color",
                   "--workspace", workspace, "--color", MANAGED_COLOR)
        if line:
            # Wait for the shell before typing at it. A new workspace opens
            # a login shell, and text sent into it before the prompt exists
            # is swallowed by the banner - the command came out spliced
            # onto "Last login:" and never ran. tmux does not have this
            # problem because there the command IS the session.
            self._await_prompt(surface)
            before = self._cmux("read-screen", "--surface", surface,
                                "--lines", "24").stdout
            if not self._type_line(surface, line, before=before):
                # Not confirmed - which is NOT the same as not started.
                # Measured 2026-08-29, three launches in a row: the line
                # was never seen whole, yet claude was running in every
                # pane (the retries had typed into it). The parent judges
                # by the session's own transcript from here; this leaves
                # the evidence it could not.
                screen = self._cmux("read-screen", "--surface", surface,
                                    "--lines", "24").stdout
                application_log("runtime", "cmux.launch_unconfirmed",
                                f"the launch line was not seen whole in "
                                f"{name!r}'s shell", severity="warning",
                                session=name, typed=line,
                                screen=screen[-1500:])
                return _Result(1, "", f"the command never landed whole in "
                                      f"{name!r}'s shell")
        if self.focus_on_create and name not in self._quiet():
            self._bring_forward(name)
        return _Result(0)

    def _quiet(self) -> set:
        """Names of sessions being launched without a window in front."""
        quiet = self.__dict__.get("_quiet_launches")
        if quiet is None:
            quiet = self.__dict__["_quiet_launches"] = set()
        return quiet

    async def launch_session(self, task_id: str, working_directory: str,
                             argv: list[str], existing: set | None = None,
                             session_id: str | None = None,
                             focus: bool = True) -> str:
        name = session_name(task_id)
        if not focus:
            self._quiet().add(name)
        try:
            return await super().launch_session(
                task_id, working_directory, argv, existing=existing,
                session_id=session_id)
        finally:
            self._quiet().discard(name)

    def bring_forward(self, session_id: str) -> None:
        """Put a running session's workspace in front of the user - the
        Boss, once the conversation has gone past small talk. Same steps
        as a worker starting; best effort for the same reason."""
        sess = self.sessions.get(session_id)
        if sess is None:
            return
        self._bring_forward(sess.name)

    # An argument this long, or with a newline in it, is not typed at the
    # shell. Measured, two launches in a row: a worker's brief - 1,500
    # characters of prose with blank lines - went in as one quoted
    # argument; cmux presses Enter for every newline, the shell went into
    # continuation lines, and the "landed whole" check below
    # (the last 24 characters, on screen) failed on the continuation
    # prompts spliced between rows. The command HAD run: the retypes
    # landed in the new Claude's input box, the launch was reported
    # failed, the worktree was deleted under a working session, and the
    # Boss started the task again - two PRs for one task. The Boss's own
    # launch never failed this way because its line ends in a UUID.
    # Such an argument goes to a file and the typed line reads it back:
    # one line, and a tail with no spaces in it.
    TYPED_ARG_MAX = 200
    LAUNCH_DIR = Path.home() / ".voice-conductor" / "launch"

    def _launch_line(self, name: str, command: list[str]) -> str:
        if len(command) <= 1:
            return command[0] if command else ""
        parts = []
        for index, part in enumerate(command):
            if "\n" in part or len(part) > self.TYPED_ARG_MAX:
                path = self._stash_argument(name, index, part)
                parts.append(f'"$(cat {shlex.quote(str(path))})"')
            else:
                parts.append(shlex.quote(part))
        return " ".join(parts)

    def _stash_argument(self, name: str, index: int, text: str) -> Path:
        self.LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
        path = self.LAUNCH_DIR / f"{name}.{index}.txt"
        path.write_text(text)
        path.chmod(0o600)
        return path

    def _bring_forward(self, name: str) -> None:
        """Put a newly started worker in front of the user.

        Starting a subagent is the one moment the user is certainly
        looking for it, and new-workspace selects the workspace inside
        cmux without raising cmux itself - so the agent started somewhere
        the user could not see. Same three steps as CmuxSurface.focus and
        for the same measured reasons; see there. Best effort: failing to
        raise a window must not fail the task that is now running.
        """
        try:
            place = self._lookup(name)
            if place is None:
                return
            workspace, _ = place
            self._cmux("select-workspace", "--workspace", workspace)
            listing = self._cmux("list-windows").stdout
            for line in listing.splitlines():
                match = re.search(
                    r"(\S+)\s+selected_workspace=(\S+)", line)
                if match and match.group(2) == workspace:
                    self._cmux("focus-window", "--window", match.group(1))
                    break
            self._raise_app()
        except Exception:
            pass

    def _raise_app(self) -> None:
        """Last resort when the window could not be named. Its own method
        so it is a seam: nothing else here leaves the socket."""
        subprocess.run(["open", "-a", "cmux"], capture_output=True)

    # How many characters at the end of a typed command must be visible on
    # screen, intact, before Enter is pressed. The session id is last in
    # the Boss's argv, and it is the part that got mangled.
    TYPED_TAIL = 24

    def _type_line(self, surface: str, line: str,
                   before: str | None = None) -> bool:
        """Type a command, look, and press Enter only once the shell shows
        it whole.

        Measured, three launches in a row: the line was typed at a prompt
        that was still settling, came out spliced through the login
        banner, and landed at the real prompt with a stray byte on the
        end - `--session-id ...d247k` - which claude refused as an
        invalid UUID. The Boss never opened; nothing said why but the
        pane. A line that did not land whole is cleared (Ctrl-U) and
        typed again, three times, and the failure is reported rather
        than left sitting at a prompt.

        before: the screen as it was at the prompt, when the caller has
        it. Then a screen that has moved on from the prompt with none of
        our line left at it counts as the command having been taken -
        measured 2026-08-29: the line went, the shell ran it, the pane
        drew claude, and the retries typed the line into claude's input
        box while the launch was reported as failed. Without a baseline
        nothing is presumed taken.
        """
        import time as _time
        tail = line[-self.TYPED_TAIL:]
        head = line[:self.TYPED_TAIL]
        for _ in range(3):
            self._cmux("send", "--surface", surface, line)
            screen = ""
            for _ in range(8):                 # up to ~4s for the echo
                _time.sleep(0.5)
                screen = self._cmux("read-screen", "--surface", surface,
                                    "--lines", "24").stdout
                if self._claude_on_screen(screen):
                    # The shell took the line and claude is up. Whatever
                    # the echo looked like, typing again now would land
                    # in claude's input box - measured, see _launch_line.
                    return True
                # A long line wraps across rows; the rows join with
                # nothing between them, and the echo may lag.
                shown = "".join(ln.rstrip() for ln in screen.splitlines()
                                if ln.strip()).rstrip()
                if shown.endswith(tail):
                    # cmux presses Enter for a newline it is asked to
                    # type; a separate send-key Enter raced long lines.
                    # And then LOOK: measured, the line landed whole, the
                    # Enter went, and the command sat at the prompt
                    # unexecuted - the shell was still settling and ate
                    # the newline. The prompt line with our command on it
                    # is still the last thing on screen until the shell
                    # takes it; press Enter again while it is.
                    for _ in range(4):
                        self._cmux("send", "--surface", surface, "\n")
                        _time.sleep(1.0)
                        screen = self._cmux("read-screen", "--surface",
                                            surface, "--lines", "24").stdout
                        rows = [ln.rstrip() for ln in screen.splitlines()
                                if ln.strip()]
                        last = "".join(rows[-6:]).rstrip()
                        if not last.endswith(tail):
                            return True             # taken: something else is drawn now
                    return True    # tried; the caller's own startup wait will judge
            if before is not None and self._taken(screen, before, head):
                return True    # gone from the prompt; the caller's startup wait will judge
            self._cmux("send-key", "--surface", surface, "ctrl+u")
            _time.sleep(0.3)
        return False

    @staticmethod
    def _taken(screen: str, before: str, head: str) -> bool:
        """Whether the shell has moved on from the prompt we typed at:
        the screen changed, our line is no longer sitting in its last
        rows, and no shell prompt ends it. A mangled line still at the
        prompt has the head in view and is retyped; an unchanged screen
        (cmux not drawing an unfocused pane) proves nothing either way."""
        if screen == before:
            return False
        rows = [ln.rstrip() for ln in screen.splitlines() if ln.strip()]
        if not rows:
            return False
        if head and head in "".join(rows[-6:]):
            return False
        return not rows[-1].endswith(("$", "%", "#", ">"))

    def _claude_on_screen(self, screen: str) -> bool:
        """The hosted CLI's own chrome, which no shell prints."""
        return self.adapter.prompt_ready(screen) or "esc to interrupt" in screen

    # The fallback's patience for a shell prompt, when the command could
    # not travel with the workspace. The shell itself takes ~2 s here;
    # what looked like a 17 s shell was cmux not rendering an unfocused
    # pane at all (see _create), which no timeout fixes.
    SHELL_PROMPT_TIMEOUT_S = 20.0

    def _await_prompt(self, surface: str,
                      timeout: float = SHELL_PROMPT_TIMEOUT_S) -> bool:
        """Wait until the shell in a new workspace is ready to be typed at.

        Looks for a prompt character at the end of the screen rather than a
        fixed sleep, so a slow login costs time and a fast one does not.
        """
        import time as _time
        deadline = _time.monotonic() + timeout
        previous, stable = None, 0
        while _time.monotonic() < deadline:
            screen = self._cmux("read-screen", "--surface", surface,
                                "--lines", "6").stdout
            lines = [ln.rstrip() for ln in screen.splitlines() if ln.strip()]
            # A prompt that is still there a second later, with nothing
            # printed after it. Measured: a login shell shows a prompt,
            # then prints its "default interactive shell is now zsh"
            # notice and a conda banner over it; text typed at the first
            # prompt was split around the banner and left unsent - three
            # launches out of six at half a second's patience, and three
            # in a row one slow evening. Two unchanged looks are needed
            # now, not one; _type_line then checks the typing itself.
            if lines and lines[-1].rstrip().endswith(("$", "%", "#", ">")):
                stable = stable + 1 if lines == previous else 0
                if stable >= 2:
                    return True
                previous = lines
            else:
                previous, stable = None, 0
            _time.sleep(0.5)
        return False


def _target(args: list[str]) -> str:
    return args[args.index("-t") + 1] if "-t" in args else ""


def _stored_password() -> str:
    from .cmux_client import PASSWORD_FILE
    try:
        return PASSWORD_FILE.read_text().strip()
    except OSError:
        return ""
