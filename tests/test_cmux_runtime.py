"""Hosting a worker in cmux, by translating one seam.

cmux cannot be a surface over the tmux runtime. CmuxSurface attaches by
resuming the provider session, and if a tmux pane still holds that session
the result is two processes on one conversation - the duplicate worker the
design forbids. Whoever owns the PTY owns the execution, so choosing cmux
is choosing a host.

Which is why this is a subclass rather than a rewrite: every pane
operation goes through one method and six verbs, so those are translated
and the rest - transcript discovery, turn events, approval detection - is
inherited. A worker in cmux should be exactly as observable as one in
tmux, because it is the same claude writing the same transcript.

Run with:  python3 -m unittest tests.test_cmux_runtime -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from conductor.cmux_runtime import (MANAGED_MARK, CmuxClaudeRuntime,
                                    _Result, scrub_prefix)

WS = "  workspace:2 WS-UUID  cond_task_a\n"
SF = "* surface:2 SF-UUID  cond_task_a\n"


def listing(*workspaces) -> str:
    return json.dumps({"workspaces": list(workspaces)})


def ours(title="cond_task_a", uuid="WS-UUID", **extra):
    row = {"id": uuid, "custom_title": title, "ref": "workspace:2",
           "description": f"{MANAGED_MARK}: {title}",
           "current_directory": "/w"}
    row.update(extra)
    return row


class Base(unittest.TestCase):
    def runtime(self, screen="$ ", known=True, workspaces=None):
        rt = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
        rt.cmux = "/fake/cmux"
        rt._password = "x"
        rt.transcript = None
        rt.places = {"cond_task_a": ("WS-UUID", "SF-UUID")} if known else {}
        self.calls = []
        rows = [ours()] if workspaces is None else list(workspaces)
        typed = {"last": ""}          # a shell echoes what is sent to it

        def cmux(*args):
            self.calls.append(args)
            if args[0] == "send":
                # Enter takes the command; anything else is typed text.
                typed["last"] = "" if args[-1] == "\n" else args[-1]
            if args[0] == "new-workspace" and "--command" in args:
                # The shell runs a creation-time command as soon as it is
                # up, and echoes it like anything typed.
                typed["last"] = args[args.index("--command") + 1]
            if args[:3] == ("workspace", "list", "--json"):
                return _Result(0, listing(*rows))
            if args[0] == "list-workspaces":
                return _Result(0, WS)
            if args[0] == "list-pane-surfaces":
                return _Result(0, SF)
            if args[0] == "read-screen":
                return _Result(0, screen + typed["last"])
            if args[0] == "list-windows":
                return _Result(
                    0, "* 0: WIN-UUID selected_workspace=WS-UUID workspaces=1")
            return _Result(0, "OK")
        rt._cmux = cmux
        self.raised = []
        rt._raise_app = lambda: self.raised.append(True)
        return rt


class OneListingPerSecond(Base):
    """_lookup lists every workspace on EVERY call, cache hit or not, to
    catch a cmux restart - and the sweep looks up every task. Measured
    against the live app: 19 lookups, 6.45s. Within a second they all say
    the same thing."""

    def listings(self):
        return [c for c in self.calls if c[:3] == ("workspace", "list",
                                                    "--json")]

    def test_lookups_within_the_ttl_share_one_listing(self):
        rt = self.runtime(known=True)
        for _ in range(5):
            rt._lookup("cond_task_a")
        self.assertEqual(len(self.listings()), 1)

    def test_the_listing_expires(self):
        rt = self.runtime(known=True)
        rt.WORKSPACES_TTL_S = 0.0
        rt._lookup("cond_task_a")
        rt._lookup("cond_task_a")
        self.assertEqual(len(self.listings()), 2)

    def test_changing_the_workspaces_drops_the_listing(self):
        """A remembered listing is wrong the moment a workspace is made
        or closed: _create looks the new one up straight away, and a
        stale listing would say cmux made nothing."""
        from unittest import mock
        rt = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
        rt.cmux, rt._password, rt.places, rt.transcript = "/fake/cmux", "x", {}, None
        done = mock.Mock(returncode=0, stdout=listing(ours()), stderr="")
        with mock.patch("conductor.cmux_runtime.subprocess.run",
                        return_value=done) as run:
            rt._workspaces()
            rt._workspaces()
            self.assertEqual(run.call_count, 1)
            rt._cmux("close-workspace", "--workspace", "WS-UUID")
            rt._workspaces()
            self.assertEqual(run.call_count, 3)   # the close, then a fresh list


class TheCommandMustLandWhole(Base):
    """Measured, three launches in a row: the Boss's line was typed at a
    prompt that was still settling and landed with a stray byte on the
    end - `--session-id ...d247k` - which claude refused as an invalid
    UUID. Enter is pressed only once the shell shows the line whole."""

    LINE = "claude --permission-mode bypassPermissions --session-id f42618f2-91dc-4935-9045-8871efb0d247"

    def typing(self, screens):
        """A runtime whose read-screen answers from `screens` in turn (the
        last repeats), recording every send."""
        rt = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
        rt.cmux, rt._password, rt.places, rt.transcript = "/fake/cmux", "x", {}, None
        self.sent, seq = [], list(screens)

        def cmux(*args):
            if args[0] == "read-screen":
                return _Result(0, seq.pop(0) if len(seq) > 1 else seq[0])
            self.sent.append(args)
            return _Result(0, "OK")
        rt._cmux = cmux
        return rt

    def test_enter_follows_a_line_shown_whole(self):
        import time
        with mock.patch.object(time, "sleep"):
            rt = self.typing(["$ ", "$ " + self.LINE[:50], "$ " + self.LINE])
            self.assertTrue(rt._type_line("SF", self.LINE))
        self.assertEqual(self.sent[0][-1], self.LINE)
        self.assertEqual(self.sent[-1][-1], "\n")
        self.assertNotIn("ctrl+u", [s[-1] for s in self.sent])

    def test_a_wrapped_line_still_counts_as_whole(self):
        import time
        wrapped = "$ " + self.LINE[:40] + "\n" + self.LINE[40:80] + "\n" + self.LINE[80:]
        with mock.patch.object(time, "sleep"):
            rt = self.typing([wrapped])
            self.assertTrue(rt._type_line("SF", self.LINE))
        self.assertEqual(self.sent[-1][-1], "\n")

    def test_enter_is_pressed_again_while_the_command_sits_at_the_prompt(self):
        """Measured: the line landed whole, Enter went, and the command
        sat at the prompt unexecuted - the settling shell ate the
        newline. The prompt line with our command is the last thing on
        screen until the shell takes it."""
        import time
        at_prompt = "$ " + self.LINE
        running = at_prompt + "\n\n Welcome to Claude Code\n\n❯ "
        # landed (1 look), still at the prompt after Enter (2 looks), then running
        with mock.patch.object(time, "sleep"):
            rt = self.typing([at_prompt, at_prompt, at_prompt, running])
            self.assertTrue(rt._type_line("SF", self.LINE))
        keys = [s[-1] for s in self.sent]
        self.assertEqual(keys.count("\n"), 3, keys)
        self.assertNotIn("ctrl+u", keys)

    def test_a_command_taken_on_the_first_enter_gets_no_second(self):
        import time
        at_prompt = "$ " + self.LINE
        running = at_prompt + "\n\n Welcome to Claude Code\n\n❯ "
        with mock.patch.object(time, "sleep"):
            rt = self.typing([at_prompt, running])
            self.assertTrue(rt._type_line("SF", self.LINE))
        self.assertEqual([s[-1] for s in self.sent].count("\n"), 1)

    def test_a_mangled_line_is_cleared_and_typed_again(self):
        import time
        mangled = "$ " + self.LINE + "k"           # what the pane showed
        with mock.patch.object(time, "sleep"):
            rt = self.typing([mangled] * 8 + ["$ " + self.LINE])
            self.assertTrue(rt._type_line("SF", self.LINE))
        keys = [s[-1] for s in self.sent]
        self.assertIn("ctrl+u", keys)
        self.assertEqual(keys.count(self.LINE), 2)
        self.assertEqual(keys[-1], "\n")

    def test_a_line_that_never_lands_is_a_failure_not_a_prompt_left_waiting(self):
        import time
        with mock.patch.object(time, "sleep"):
            rt = self.typing(["$ garbage"])
            self.assertFalse(rt._type_line("SF", self.LINE))
        keys = [s[-1] for s in self.sent]
        self.assertEqual(keys.count(self.LINE), 3)
        self.assertNotIn("\n", keys, "Enter was pressed on a line that never landed")

    def test_a_line_the_shell_took_is_not_typed_into_what_it_started(self):
        """Measured 2026-08-29: the echo was missed, the shell had run
        the line anyway, the pane was drawing claude - and the retries
        typed the launch line into claude's input box while the launch
        was reported failed. A screen that has moved on from the prompt,
        with none of our line left at it, is the command taken."""
        import time
        booting = "\n".join(["Last login: Sat Aug 29 00:30:25",
                             "", "Loading claude…"])   # no chrome yet
        with mock.patch.object(time, "sleep"):
            rt = self.typing([booting])
            self.assertTrue(rt._type_line("SF", self.LINE, before="$ "))
        keys = [s[-1] for s in self.sent]
        self.assertEqual(keys.count(self.LINE), 1)
        self.assertNotIn("ctrl+u", keys)
        self.assertNotIn("\n", keys)

    def test_a_mangled_line_at_the_prompt_is_still_retyped_with_a_baseline(self):
        import time
        mangled = "$ " + self.LINE + "k"
        with mock.patch.object(time, "sleep"):
            rt = self.typing([mangled] * 8 + ["$ " + self.LINE])
            self.assertTrue(rt._type_line("SF", self.LINE, before="$ "))
        keys = [s[-1] for s in self.sent]
        self.assertIn("ctrl+u", keys)
        self.assertEqual(keys[-1], "\n")

    def test_an_unchanged_screen_proves_nothing(self):
        """cmux does not draw an unfocused pane; a stale screen is not
        the command taken, and the caller decides by the transcript."""
        import time
        with mock.patch.object(time, "sleep"):
            rt = self.typing(["$ "])
            self.assertFalse(rt._type_line("SF", self.LINE, before="$ "))
        self.assertNotIn("\n", [s[-1] for s in self.sent])


BRIEF = ("Your workspace: /Users/x/.voice-conductor/workspaces/proj_1/task_a\n"
         "Your branch: agent/task_a (already checked out)\n\n"
         "Your task: Cap generated title length\n\n"
         "Goal: The user says: \"Let's also work on, actually just another small "
         "PR: instead of having to truncate our title with the ellipsis - why "
         "don't we just have it so the generated titles have to be under a "
         "certain length.\"\n\nContext a worker starting cold needs: find where "
         "titles are generated. Keep it a small PR, and open a PR when done.\n\n"
         "This machine is in use by the user right now. Do not play audio, use "
         "the speakers or the microphone, open windows, take focus, or send "
         "keystrokes to other apps; if a real device is the only way to verify "
         "something, report that and stop.")


class TheBriefIsNotTyped(Base):
    """Measured, two launches in a row: a worker's brief went in as one
    quoted argument with newlines; cmux pressed Enter at each, zsh went
    into continuation lines, the landed-whole check failed on them, the
    retypes landed in the new claude's input box, the launch was
    reported failed and the worktree deleted under a working session.
    The Boss's line never failed this way: it ends in a UUID."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def launch(self, *command):
        rt = self.runtime()
        rt.LAUNCH_DIR = Path(self.tmp.name)
        rt._await_prompt = lambda *a, **k: True
        out = rt._tmux("new-session", "-d", "-s", "cond_task_a", "-c", "/w", *command)
        sent = [c for c in self.calls if c[0] == "send"]
        return rt, out, sent[0][-1]

    def test_an_argument_with_newlines_goes_to_a_file(self):
        rt, out, line = self.launch("/bin/claude", "--permission-mode", "auto", BRIEF)
        self.assertEqual(out.returncode, 0)
        self.assertNotIn("\n", line, "a newline typed is an Enter pressed")
        self.assertIn('/bin/claude --permission-mode auto "$(cat ', line)
        path = Path(self.tmp.name) / "cond_task_a.3.txt"
        self.assertEqual(path.read_text(), BRIEF, "the brief, verbatim")
        self.assertIn(str(path), line)

    def test_the_typed_tail_has_no_spaces_to_wrap_on(self):
        _, _, line = self.launch("/bin/claude", "--permission-mode", "auto", BRIEF)
        self.assertNotIn(" ", line[-CmuxClaudeRuntime.TYPED_TAIL:])

    def test_a_short_argument_is_still_typed_inline(self):
        _, _, line = self.launch("/bin/claude", "--permission-mode", "auto", "go")
        self.assertIn("/bin/claude --permission-mode auto go", line)
        self.assertNotIn("$(cat", line)

    def test_a_long_one_line_argument_goes_to_a_file_too(self):
        _, _, line = self.launch("/bin/claude", "x" * 300)
        self.assertIn("$(cat", line)
        self.assertNotIn("x" * 300, line)

    def test_the_file_is_private(self):
        self.launch("/bin/claude", BRIEF)
        mode = (Path(self.tmp.name) / "cond_task_a.1.txt").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class AClaudeAlreadyRunningIsNotTypedInto(TheCommandMustLandWhole):
    """The other half of the same incident: whatever the echo looked
    like, once claude's chrome is on screen the line was taken, and
    typing again lands in claude's input box."""

    def test_claudes_chrome_means_taken(self):
        import time
        chrome = "$ " + self.LINE[:30] + "\n\n Welcome to Claude Code\n\n❯ \n" \
                 "  ⏵⏵ auto mode on (shift+tab to cycle)"
        with mock.patch.object(time, "sleep"):
            rt = self.typing(["$ ", chrome])
            self.assertTrue(rt._type_line("SF", self.LINE))
        keys = [s[-1] for s in self.sent]
        self.assertEqual(keys.count(self.LINE), 1, "typed once")
        self.assertNotIn("ctrl+u", keys)
        self.assertNotIn("\n", keys, "no Enter into claude's box either")

    def test_the_measured_continuation_prompts_then_claude(self):
        """The shell's continuation prompts split the tail; then claude came up."""
        import time
        quoted = "$ claude --permission-mode auto 'Your task: x\nquote> Goal: y\n" \
                 "quote> report that and stop.'"
        chrome = quoted + "\n\n❯ \n  esc to interrupt"
        with mock.patch.object(time, "sleep"):
            rt = self.typing([quoted, quoted, chrome])
            self.assertTrue(rt._type_line("SF", "claude --permission-mode auto 'Your task: x\nGoal: y\nreport that and stop.'"))
        keys = [s[-1] for s in self.sent]
        self.assertNotIn("ctrl+u", keys)
        self.assertEqual(len([k for k in keys if k.startswith("claude")]), 1)


class AWindowWhenThereIsNone(Base):
    """cmux drops its window when the last workspace closes, and then
    cannot make a workspace until it has one ("TabManager not
    available"). Measured: every workspace closed to start clean, and the
    Boss could not be opened for the next ten minutes."""

    def test_no_window_is_repaired_with_one_and_the_command_retried(self):
        from unittest import mock
        from conductor.cmux_setup import needs_window
        self.assertTrue(needs_window("Error: unavailable: TabManager not available"))
        self.assertFalse(needs_window("Error: invalid_params: no such workspace"))
        rt = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
        rt.cmux, rt._password, rt.places, rt.transcript = "/fake/cmux", "x", {}, None
        answers = [mock.Mock(returncode=1, stdout="",
                             stderr="Error: unavailable: TabManager not available"),
                   mock.Mock(returncode=0, stdout="OK window:1", stderr=""),
                   mock.Mock(returncode=0, stdout="OK workspace:1", stderr="")]
        with mock.patch("conductor.cmux_runtime.subprocess.run",
                        side_effect=answers) as run:
            out = rt._cmux("new-workspace", "--name", "cond_boss", "--cwd", "/w")
        self.assertEqual(out.returncode, 0)
        argv = [c.args[0][1:] for c in run.call_args_list]
        self.assertEqual(argv[1], ["new-window"])
        self.assertEqual(argv[2][:2], ["new-workspace", "--name"])

    def test_it_is_tried_once_not_for_ever(self):
        from unittest import mock
        rt = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
        rt.cmux, rt._password, rt.places, rt.transcript = "/fake/cmux", "x", {}, None
        always = mock.Mock(returncode=1, stdout="",
                           stderr="Error: unavailable: TabManager not available")
        with mock.patch("conductor.cmux_runtime.subprocess.run",
                        return_value=always) as run:
            out = rt._cmux("new-workspace", "--name", "cond_boss")
        self.assertEqual(out.returncode, 1)
        self.assertLessEqual(run.call_count, 3)


class NamedKeysInCmuxSpelling(Base):
    def test_ctrl_u_clears_the_box_here_too(self):
        """The parent sets a draft aside with tmux's C-u; cmux's send-key
        spells it ctrl+u and would have typed the letters otherwise."""
        rt = self.runtime(known=True)
        rt._tmux("send-keys", "-t", "cond_task_a", "C-u")
        self.assertIn(("send-key", "--surface", "SF-UUID", "ctrl+u"),
                      self.calls)

    def test_enter_still_passes_through(self):
        rt = self.runtime(known=True)
        rt._tmux("send-keys", "-t", "cond_task_a", "Enter")
        self.assertIn(("send-key", "--surface", "SF-UUID", "Enter"),
                      self.calls)


class TranslatingTheSeam(Base):
    def test_capture_pane_reads_the_surface(self):
        rt = self.runtime(screen="hello from the worker")
        out = rt._tmux("capture-pane", "-t", "cond_task_a", "-p")
        self.assertEqual(out.returncode, 0)
        self.assertIn("hello from the worker", out.stdout)
        self.assertIn(("read-screen", "--surface", "SF-UUID",
                       "--scrollback", "--lines", "60"), self.calls)

    def test_literal_text_is_sent_not_interpreted(self):
        """A message containing "Enter" or "-v" must arrive as text."""
        rt = self.runtime()
        rt._tmux("send-keys", "-t", "cond_task_a", "-l", "run tests -v")
        self.assertIn(("send", "--surface", "SF-UUID", "run tests -v"),
                      self.calls)

    def test_named_keys_go_through_send_key(self):
        rt = self.runtime()
        rt._tmux("send-keys", "-t", "cond_task_a", "Enter")
        self.assertIn(("send-key", "--surface", "SF-UUID", "Enter"),
                      self.calls)

    def test_tmux_key_names_are_translated(self):
        """The parent presses BSpace to take its probe character back;
        cmux calls that key backspace and rejects the tmux spelling."""
        rt = self.runtime()
        rt._tmux("send-keys", "-t", "cond_task_a", "BSpace")
        self.assertIn(("send-key", "--surface", "SF-UUID", "backspace"),
                      self.calls)

    def test_has_session_asks_whether_the_workspace_exists(self):
        self.assertEqual(
            self.runtime()._tmux("has-session", "-t", "cond_task_a").returncode, 0)
        self.assertEqual(
            self.runtime(known=False)._tmux(
                "has-session", "-t", "nothing").returncode, 1)

    def test_kill_session_closes_the_workspace_and_forgets_it(self):
        rt = self.runtime()
        rt._tmux("kill-session", "-t", "cond_task_a")
        self.assertIn(("close-workspace", "--workspace", "WS-UUID"),
                      self.calls)
        self.assertNotIn("cond_task_a", rt.places)

    def test_an_unknown_target_fails_rather_than_guessing(self):
        """Sending to whatever happens to be focused is how a follow-up
        reaches the wrong worker."""
        rt = self.runtime(known=False)
        rt._cmux = lambda *a: _Result(0, "")     # nothing listed
        out = rt._tmux("send-keys", "-t", "cond_task_zzz", "-l", "hi")
        self.assertEqual(out.returncode, 1)
        self.assertIn("no cmux workspace", out.stderr)


class WhoseWorkspaceIsIt(Base):
    """§15. Nothing we did not create may be driven, and nothing we did
    create may be missed."""

    def test_a_personal_workspace_with_our_name_is_not_adopted(self):
        """Driving it would type into somebody's own terminal, and
        kill-session would close it under them."""
        personal = {"id": "THEIRS", "custom_title": "cond_task_a",
                    "ref": "workspace:9", "description": None,
                    "current_directory": "/Users/x/code"}
        rt = self.runtime(known=False, workspaces=[personal])
        self.assertIsNone(rt._lookup("cond_task_a"))

    def test_a_workspace_from_before_the_marker_is_still_ours(self):
        """Refusing these would orphan live workers across the upgrade,
        and the parent would resume the session somewhere else - two
        processes on one conversation, which is the thing we never do."""
        rt = self.runtime(known=False, workspaces=[
            {"id": "WS-UUID", "custom_title": "cond_task_a",
             "ref": "workspace:2", "description": None,
             "current_directory": "/home/.voice-conductor/workspaces/p/t"}])
        rt.transcript = mock.Mock(dir="/home/.voice-conductor/executions")
        self.assertEqual(rt._lookup("cond_task_a"), ("WS-UUID", "SF-UUID"))

    def test_the_same_name_somewhere_else_is_not_ours(self):
        rt = self.runtime(known=False, workspaces=[
            {"id": "THEIRS", "custom_title": "cond_task_a",
             "ref": "workspace:9", "description": None,
             "current_directory": "/Users/x/notes"}])
        rt.transcript = mock.Mock(dir="/home/.voice-conductor/executions")
        self.assertIsNone(rt._lookup("cond_task_a"))


class WhenCmuxItselfRestarts(Base):
    """§32. Closing a workspace kills the process it hosts, so cmux going
    away takes every worker with it - but the provider SESSIONS survive
    and resume. What must not happen is the runtime insisting on uuids
    from the dead instance."""

    def test_a_remembered_workspace_that_no_longer_exists_is_forgotten(self):
        rt = self.runtime(known=True, workspaces=[])   # cmux came back empty
        self.assertIsNone(rt._lookup("cond_task_a"))
        self.assertNotIn("cond_task_a", rt.places)

    def test_a_dead_workspace_reads_as_a_dead_session(self):
        """has-session is what the parent asks before deciding a worker
        needs recovering. Answering yes from a stale cache would send
        follow-ups into a workspace that no longer exists."""
        rt = self.runtime(known=True, workspaces=[])
        self.assertEqual(rt._tmux("has-session", "-t", "cond_task_a")
                         .returncode, 1)

    def test_a_worker_is_found_again_after_cmux_reassigns_ids(self):
        """The names we assign are the only identity that survives, which
        is why the workspace is titled for the session."""
        rt = self.runtime(known=True,
                          workspaces=[ours(uuid="NEW-UUID-AFTER-RESTART")])
        self.assertEqual(rt._lookup("cond_task_a"),
                         ("NEW-UUID-AFTER-RESTART", "SF-UUID"))


class LaunchingAWorker(Base):
    def test_the_workspace_is_named_for_the_session(self):
        """The parent's bookkeeping keys on that name, and it is how a
        workspace is found again after a restart - cmux knows nothing of
        our session names otherwise."""
        rt = self.runtime()
        rt._await_prompt = lambda *a, **k: True
        rt._tmux("new-session", "-d", "-s", "cond_task_a", "-c", "/w",
                 "/bin/claude", "--permission-mode", "auto", "do it")
        made = next(c for c in self.calls if c[0] == "new-workspace")
        self.assertEqual(made[made.index("--name") + 1], "cond_task_a")
        self.assertEqual(made[made.index("--cwd") + 1], "/w")

    def test_a_workspace_we_create_is_marked_as_ours(self):
        """Identity by title alone would adopt a personal workspace the
        user happened to name the same thing."""
        rt = self.runtime()
        rt._await_prompt = lambda *a, **k: True
        rt._tmux("new-session", "-d", "-s", "cond_task_a", "-c", "/w",
                 "/bin/claude", "go")
        made = next(c for c in self.calls if c[0] == "new-workspace")
        self.assertIn(MANAGED_MARK, made[made.index("--description") + 1])
        self.assertIn(("workspace-action", "--action", "set-color",
                       "--workspace", "WS-UUID", "--color", "Teal"),
                      self.calls)

    def test_the_pane_is_woken_then_the_command_typed_at_its_prompt(self):
        """Measured: cmux does not render an unfocused workspace until it
        is focused or handed input, so waiting for its prompt waited the
        whole timeout on every launch. An empty creation-time command
        wakes it; the real command still goes in at a stable prompt and
        is checked whole - a creation-time command arrives before the
        login shell is ready and is lost."""
        rt = self.runtime()
        waited = []
        rt._await_prompt = lambda *a, **k: waited.append(True) or True
        rt._tmux("new-session", "-d", "-s", "cond_task_a", "-c", "/w",
                 "/bin/claude", "--permission-mode", "auto", "go")
        made = next(c for c in self.calls if c[0] == "new-workspace")
        self.assertEqual(made[made.index("--command") + 1], "")
        self.assertTrue(waited, "typed into the shell before it was ready")
        sent = [c for c in self.calls if c[0] == "send"]
        self.assertIn("/bin/claude --permission-mode auto go", sent[0][-1])

    def test_an_older_cmux_without_command_still_launches(self):
        rt = self.runtime()
        rt._await_prompt = lambda *a, **k: True
        inner = rt._cmux
        rt._cmux = lambda *a: (_Result(1, "", "unknown flag: --command")
                               if a[0] == "new-workspace" and "--command" in a
                               else inner(*a))
        out = rt._tmux("new-session", "-d", "-s", "cond_task_a", "-c", "/w",
                       "/bin/claude", "go")
        self.assertEqual(out.returncode, 0)
        self.assertTrue([c for c in self.calls if c[0] == "send"])

    def test_a_started_worker_is_brought_to_the_front(self):
        """new-workspace selects the workspace inside cmux without raising
        cmux itself, so an agent the user just asked for started somewhere
        they could not see."""
        rt = self.runtime()
        rt._await_prompt = lambda *a, **k: True
        rt._tmux("new-session", "-d", "-s", "cond_task_a", "-c", "/w",
                 "/bin/claude", "--permission-mode", "auto", "go")
        self.assertIn(("focus-window", "--window", "WIN-UUID"), self.calls)
        self.assertTrue(self.raised, "focus-window alone is not reliable")

    def test_bringing_a_worker_forward_cannot_fail_the_task(self):
        """The task is already running by then. A window that will not
        raise is a worse view of a working agent, not a failed start."""
        rt = self.runtime()
        rt._await_prompt = lambda *a, **k: True
        rt._raise_app = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        inner = rt._cmux
        rt._cmux = lambda *a: (_Result(0, "") if a[0] == "list-windows"
                               else inner(*a))
        out = rt._tmux("new-session", "-d", "-s", "cond_task_a", "-c", "/w",
                       "/bin/claude", "go")
        self.assertEqual(out.returncode, 0)

    def test_the_worker_environment_is_scrubbed(self):
        """cmux inherits whatever launched it. If that was a Claude Code
        session, CLAUDE_CODE_CHILD_SESSION comes with it and Claude Code
        turns transcript saving OFF - the file every part of our
        supervision reads. create_session then times out waiting for a
        session file that will never be written."""
        rt = self.runtime()
        rt._await_prompt = lambda *a, **k: True
        rt._tmux("new-session", "-d", "-s", "cond_task_a", "-c", "/w",
                 "/bin/claude", "--permission-mode", "auto", "go")
        sent = [c for c in self.calls if c[0] == "send"]
        self.assertTrue(sent)
        self.assertIn("CLAUDE_CODE_CHILD_SESSION", sent[0][-1])
        self.assertIn("ANTHROPIC_API_KEY", sent[0][-1])

    def test_an_unconfirmed_launch_leaves_the_screen_in_the_log(self):
        """The runtime never logged what the pane showed, so three
        false failures could not be diagnosed from the logs at all."""
        rt = self.runtime(screen="$ garbage")
        rt._await_prompt = lambda *a, **k: True
        inner = rt._cmux
        rt._cmux = lambda *a: (_Result(0, "$ garbage") if a[0] == "read-screen"
                               else inner(*a))
        with mock.patch("conductor.cmux_runtime.application_log") as log, \
                mock.patch("conductor.cmux_runtime.time.sleep"):
            out = rt._tmux("new-session", "-d", "-s", "cond_task_a", "-c", "/w",
                           "/bin/claude", "go")
        self.assertEqual(out.returncode, 1)
        self.assertIn("never landed whole", out.stderr)
        events = [c.args[1] for c in log.call_args_list]
        self.assertIn("cmux.launch_unconfirmed", events)

    def test_the_scrub_names_the_markers_that_matter(self):
        prefix = scrub_prefix("cond_task_a")
        for marker in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_CHILD_SESSION",
                       "CLAUDE_CODE_SESSION_ID"):
            self.assertIn(marker, prefix)

    def test_the_scrub_tells_the_worker_which_task_it_is(self):
        """The GUI lease is asked for by task id, and a worker's shell is
        the only place that id can come from."""
        prefix = scrub_prefix("cond_task_a", home="/tmp/home")
        self.assertIn("VOICE_CONDUCTOR_TASK_ID=task_a", prefix)
        self.assertIn("VOICE_CONDUCTOR_HOME=/tmp/home", prefix)


class OneSeamForEveryPaneOperation(unittest.TestCase):
    def test_sends_are_not_run_against_real_tmux(self):
        """They were: _send_argv returned argv beginning with "tmux" and
        the caller ran it directly, so a follow-up to a cmux-hosted worker
        went to tmux, which reported a pane it had never heard of."""
        from conductor.tmux_runtime import TmuxClaudeRuntime
        argv = TmuxClaudeRuntime._send_argv("cond_task_1", "hello")
        self.assertNotIn("tmux", [part for row in argv for part in row])

    def test_the_runtime_is_chosen_by_the_selected_surface(self):
        import boss
        import conduct
        from pathlib import Path
        with mock.patch.object(boss, "WORKER_SURFACE", "cmux"):
            built = conduct.build_conductor(Path.home() / ".voice-conductor", [])
        self.assertEqual(type(built.runtime.runtimes["local"]).__name__,
                         "CmuxClaudeRuntime")

    def test_the_cmux_surface_is_one_the_conductor_will_actually_pick(self):
        """Registering it is not enough. The Conductor walks the provider
        preference order and takes the first REGISTERED name, so a
        surface registered under a name no order mentions is dead code and
        the click fails with no surface at all."""
        import boss
        import conduct
        from pathlib import Path
        with mock.patch.object(boss, "WORKER_SURFACE", "cmux"):
            built = conduct.build_conductor(Path.home() / ".voice-conductor", [])
        for provider in ("claude-code", "codex"):
            order = built.surface_preference.order_for(provider)
            self.assertTrue(
                [name for name in order if name in built.surfaces],
                f"no registered surface in {provider} order {order}")

    def test_terminal_is_available_behind_a_flag(self):
        """cmux is the product's surface, so it is what you get without
        saying anything. A machine without cmux still has a way in that
        does not involve editing boss.py."""
        import conduct
        from pathlib import Path
        built = conduct.build_conductor(Path.home() / ".voice-conductor", [],
                                        worker_surface="terminal")
        self.assertEqual(type(built.runtime.runtimes["local"]).__name__,
                         "TmuxClaudeRuntime")
        self.assertIn("interactive-terminal", built.surfaces)

    def test_embedded_terminals_register_no_external_surface(self):
        """The Boss window draws every worker's terminal itself; a
        Terminal.app window opening beside it would be a second copy of a
        session the window already shows."""
        import conduct
        from pathlib import Path
        built = conduct.build_conductor(Path.home() / ".voice-conductor", [],
                                        worker_surface="terminal",
                                        embedded_terminals=True)
        self.assertEqual(built.surfaces, {})

    def test_the_raise_lands_on_the_process_that_owns_the_window(self):
        """uv run wraps the GUI process, and System Events cannot see the
        wrapper: the frontmost call must name the deepest descendant."""
        import subprocess
        import conduct
        children = {"100": "200\n", "200": "300\n", "300": ""}
        commands = []

        def fake_run(args, **kwargs):
            commands.append(args)
            if args[0] == "pgrep":
                return subprocess.CompletedProcess(
                    args, 0, stdout=children[args[-1]], stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        with mock.patch.object(conduct.subprocess, "run", fake_run):
            conduct.raise_boss_window(100)
        self.assertIn("unix id is 300", commands[-1][-1])

    def test_the_flag_beats_the_configured_default(self):
        import boss
        import conduct
        from pathlib import Path
        with mock.patch.object(boss, "WORKER_SURFACE", "terminal"):
            built = conduct.build_conductor(Path.home() / ".voice-conductor",
                                            [], worker_surface="cmux")
        self.assertEqual(type(built.runtime.runtimes["local"]).__name__,
                         "CmuxClaudeRuntime")

    def test_cmux_is_what_you_get_without_asking(self):
        import boss
        self.assertEqual(boss.WORKER_SURFACE, "cmux")

    def test_the_terminal_default_still_resolves_to_a_surface(self):
        import boss
        import conduct
        from pathlib import Path
        with mock.patch.object(boss, "WORKER_SURFACE", "terminal"):
            built = conduct.build_conductor(Path.home() / ".voice-conductor", [])
        order = built.surface_preference.order_for("claude-code")
        self.assertIn("interactive-terminal", order)
        self.assertIn("interactive-terminal", built.surfaces)


class ANewlineIsNotEnter(Base):
    def test_a_multi_line_message_arrives_as_one_submission(self):
        """cmux `send` presses Enter for every newline, real or escaped.
        Measured with the Boss: its multi-line context went in as
        fragments and it answered the first one. tmux's -l typed the text
        verbatim; the cmux seam has to make the lines one."""
        rt = self.runtime()
        rt._tmux("send-keys", "-t", "cond_task_a", "-l",
                 "Current time: now\n\nKnown projects:\n- posely\nUser says: go")
        sent = next(c for c in self.calls if c[0] == "send")
        text = sent[-1]
        self.assertNotIn("\n", text)
        self.assertNotIn("\\n", text)
        self.assertTrue(text.endswith("User says: go"))
        self.assertIn("Known projects:", text)

    def test_a_tab_is_not_the_tab_key(self):
        """cmux `send` presses TAB for a tab, real or escaped, which moves
        focus in Claude Code's input box and scrambles the characters
        typed around it. A space stands in for it, like a newline."""
        rt = self.runtime()
        rt._tmux("send-keys", "-t", "cond_task_a", "-l",
                 "jq -r '\"\\(.a)\\t\\(.b)\"'\tdone")
        sent = next(c for c in self.calls if c[0] == "send")
        text = sent[-1]
        self.assertNotIn("\t", text)
        self.assertNotIn("\\t", text)
        self.assertTrue(text.endswith("done"))
        self.assertIn("jq -r", text)


class TheTitleIsALabelNotIdentity(Base):
    """dress() renames a workspace to the task's own words. The stable
    session name stays in the description stamp, so nothing renamed is
    ever lost - and a user's own workspace still answers only to its
    title."""

    def test_a_renamed_workspace_is_still_found(self):
        rt = self.runtime(known=False, workspaces=[
            ours(custom_title="posely: Fix login flake")])
        self.assertEqual(rt._lookup("cond_task_a"), ("WS-UUID", "SF-UUID"))

    def test_list_sessions_reports_the_stable_name(self):
        rt = self.runtime(known=False, workspaces=[
            ours(custom_title="posely: Fix login flake")])
        out = rt._tmux("list-sessions")
        self.assertEqual(out.stdout.strip(), "cond_task_a")

    def test_an_unstamped_workspace_answers_to_its_title(self):
        rt = self.runtime(known=False, workspaces=[
            {"id": "WS-UUID", "custom_title": "cond_task_a",
             "ref": "workspace:2", "description": None,
             "current_directory": "/home/.voice-conductor/workspaces/p/t"}])
        rt.transcript = mock.Mock(dir="/home/.voice-conductor/executions")
        self.assertEqual(rt._lookup("cond_task_a"), ("WS-UUID", "SF-UUID"))


class DressingTheWorkspace(Base):
    """The sidebar as a routing map: title, colour, pin, flash."""

    def actions(self):
        return [c for c in self.calls if c[0] == "workspace-action"]

    def test_dress_titles_colours_pins_and_flashes(self):
        rt = self.runtime(known=True)
        self.assertTrue(rt.dress("cond_task_a", title="posely: Fix login",
                                 state="attention", flash=True, pin=True))
        rename = next(c for c in self.actions() if "rename" in c)
        self.assertIn("posely: Fix login", rename)
        colour = next(c for c in self.actions() if "set-color" in c)
        self.assertIn("Orange", colour)
        self.assertTrue(any("pin" in c for c in self.actions()))
        flash = next(c for c in self.calls if c[0] == "trigger-flash")
        self.assertIn("WS-UUID", flash)
        self.assertIn("SF-UUID", flash)

    def test_a_state_also_writes_the_progress_label(self):
        """The custom sidebar reads workspace progress, not status pills,
        so the delegation loop is written there too."""
        rt = self.runtime(known=True)
        self.assertTrue(rt.dress("cond_task_a", state="working"))
        bar = next(c for c in self.calls if c[0] == "set-progress")
        self.assertIn("working \u2014 reports back to Boss", bar)
        self.assertIn("WS-UUID", bar)

    def test_a_status_pill_is_set_under_the_conductor_key(self):
        rt = self.runtime(known=True)
        self.assertTrue(rt.dress("cond_task_a",
                                 status="\u25c0 Boss 14:32"))
        pill = next(c for c in self.calls if c[0] == "set-status")
        self.assertIn("conductor", pill)
        self.assertIn("\u25c0 Boss 14:32", pill)
        self.assertIn("WS-UUID", pill)

    def test_only_what_was_asked_for_is_touched(self):
        rt = self.runtime(known=True)
        self.assertTrue(rt.dress("cond_task_a", state="done"))
        self.assertFalse(any("rename" in c for c in self.actions()))
        self.assertFalse(any("pin" in c for c in self.actions()))
        colour = next(c for c in self.actions() if "set-color" in c)
        self.assertIn("Green", colour)

    def test_a_missing_workspace_cannot_be_dressed(self):
        rt = self.runtime(known=False, workspaces=[])
        self.assertFalse(rt.dress("cond_task_a", title="x", state="working"))
        self.assertEqual(self.actions(), [])


class ShowingTheDelegationSidebar(Base):
    """select_sidebar puts the conductor's custom sidebar in cmux's left
    sidebar picker - once, on first install; after that the choice is the
    user's."""

    def test_the_sidebar_is_selected_by_name(self):
        rt = self.runtime()
        self.assertTrue(rt.select_sidebar("conductor"))
        self.assertIn(("sidebar", "select", "conductor"), self.calls)


if __name__ == "__main__":
    unittest.main()
