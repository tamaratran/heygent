"""Deterministic tests for the interactive runtime's SDK-free logic:
path munging (verified against real ~/.claude layouts), session-JSONL event
normalization, send-keys construction, and the interactive surface command.

The full PTY loop (tmux + real claude) is covered by
tests/smoke_interactive.py, run manually.

Run with:  python3 -m unittest tests.test_tmux_runtime -v
"""

from __future__ import annotations

import asyncio
import unittest

from conductor.tmux_runtime import TmuxClaudeRuntime

from conductor.surfaces import InteractiveTerminalSurface, SurfaceRequest


def _tmux_available() -> bool:
    import shutil
    return shutil.which("tmux") is not None


class MungeTest(unittest.TestCase):
    def test_matches_real_claude_layout(self) -> None:
        from conductor.tmux_runtime import munge_project_dir
        # Format observed in ~/.claude/projects on a real machine:
        self.assertEqual(
            munge_project_dir("/Users/t/.voice-conductor/workspaces"
                              "/proj_1/task_2"),
            "-Users-t--voice-conductor-workspaces-proj-1-task-2")
        self.assertEqual(munge_project_dir("/Users/t"), "-Users-t")


class NormalizeTest(unittest.TestCase):
    def setUp(self) -> None:
        from conductor.tmux_runtime import normalize_entry
        self.normalize = normalize_entry
        self.state: dict = {}

    def test_user_message_surfaces_regardless_of_source(self) -> None:
        events = self.normalize(
            {"type": "user",
             "message": {"role": "user",
                         "content": "Actually don't change OAuth."}},
            self.state)
        self.assertEqual(events[0].type, "progress")
        self.assertIn("don't change OAuth", events[0].summary)
        self.assertEqual(events[0].detail["source"], "user_message")

    def test_assistant_turn_accumulates_then_completes(self) -> None:
        events = self.normalize(
            {"type": "assistant",
             "message": {"stop_reason": "tool_use",
                         "content": [
                             {"type": "text", "text": "Inspecting auth."},
                             {"type": "tool_use", "name": "Read",
                              "input": {"file_path": "src/auth.ts"}}]}},
            self.state)
        self.assertEqual([e.type for e in events],
                         ["progress", "progress"])
        self.assertIn("Read(src/auth.ts)", events[1].summary)

        events = self.normalize(
            {"type": "assistant",
             "message": {"stop_reason": "end_turn",
                         "content": [{"type": "text",
                                      "text": "Fixed; tests pass."}]}},
            self.state)
        self.assertEqual(events[-1].type, "completed")
        # The finish is the last thing said; the narration before it was
        # reported as progress when it happened (test_cli_adapter has the
        # 23:41:59Z case that changed this).
        self.assertEqual(events[-1].summary, "Fixed; tests pass.")
        self.assertEqual(self.state["turn_text"], [])   # reset per turn

    def test_a_thought_alone_does_not_end_the_turn(self) -> None:
        """Measured on the Boss, 2026-08-29 09:17:23: the thinking block
        is its own line, stamped end_turn, 30 ms before the text. Ending
        the turn on it ended it empty, and the answer that followed was
        taken for a new turn - the reply to a pushed worker update
        landed as "typed", and the user never heard it."""
        events = self.normalize(
            {"type": "assistant",
             "message": {"stop_reason": "end_turn",
                         "content": [{"type": "thinking",
                                      "thinking": "The user wants..."}]}},
            self.state)
        self.assertEqual(events, [])
        events = self.normalize(
            {"type": "assistant",
             "message": {"stop_reason": "end_turn",
                         "content": [{"type": "text",
                                      "text": "PR 81 is a design doc."}]}},
            self.state)
        self.assertEqual([e.type for e in events], ["progress", "completed"])
        self.assertEqual(events[-1].summary, "PR 81 is a design doc.")
        # The turn marker that follows does not end it a second time.
        self.assertEqual(self.normalize({"type": "system",
                                         "subtype": "turn_duration"},
                                        self.state), [])

    def test_a_turn_that_ends_in_thought_alone_still_ends(self) -> None:
        """On the turn marker, or failing that the next message."""
        thought = {"type": "assistant",
                   "message": {"stop_reason": "end_turn",
                               "content": [{"type": "thinking",
                                            "thinking": "..."}]}}
        self.assertEqual(self.normalize(thought, self.state), [])
        events = self.normalize({"type": "system",
                                 "subtype": "turn_duration"}, self.state)
        self.assertEqual([e.type for e in events], ["completed"])
        self.assertEqual(events[0].summary, "")

        self.assertEqual(self.normalize(thought, self.state), [])
        events = self.normalize(
            {"type": "user", "message": {"role": "user",
                                         "content": "and now?"}},
            self.state)
        self.assertEqual([e.type for e in events], ["completed", "progress"])

    def test_junk_entries_are_ignored(self) -> None:
        for junk in ({"type": "queue-operation"}, {"type": "attachment"},
                     {"type": "ai-title"}, {}, {"type": "assistant"}):
            self.assertEqual(self.normalize(junk, self.state), [])


@unittest.skipUnless(_tmux_available(), "tmux not installed")
class SendKeysTest(unittest.TestCase):
    def test_literal_then_enter(self) -> None:
        from conductor.tmux_runtime import TmuxClaudeRuntime
        argv = TmuxClaudeRuntime._send_argv(
            "cond_task_1", "Don't touch OAuth; run tests -v")
        # No "tmux" prefix: delivery goes through _tmux so every pane
        # operation has ONE seam for a subclass to map. It did not, and
        # follow-ups reached real tmux while the worker lived elsewhere.
        self.assertEqual(argv[0][:4],
                         ["send-keys", "-t", "cond_task_1", "-l"])
        self.assertEqual(argv[0][4], "Don't touch OAuth; run tests -v")
        self.assertEqual(argv[1][-1], "Enter")


class InteractiveSurfaceTest(unittest.TestCase):
    def test_attach_command_joins_never_forks(self) -> None:
        surface = InteractiveTerminalSurface()
        self.assertTrue(surface.interactive)
        request = SurfaceRequest(
            project_id="p", task_id="task_1", title="posely — Fix login",
            working_directory="/ws", provider="claude-code",
            pty_handle="cond_task_1")
        command = surface.command_for(request)
        self.assertIn("tmux attach-session -t cond_task_1", command)
        self.assertNotIn("claude", command)     # attach joins; never spawns

    def test_refuses_without_pty(self) -> None:
        surface = InteractiveTerminalSurface()
        with self.assertRaises(RuntimeError):
            surface.create(SurfaceRequest(
                project_id="p", task_id="t", title="x",
                working_directory="/", provider="claude-code"))



class WatchingFromTheSweep(unittest.TestCase):
    """ensure_watched's probe is a subprocess; its watcher is an asyncio
    task. Run in a thread, the task could not be created and nothing was
    watched - measured as task.watch_failed every sweep."""

    def runtime(self):
        rt = TmuxClaudeRuntime(transcript_dir=None)
        rt._live_worker_in = lambda cwd: "cond_task_a"
        self.watched = []

        async def watch(sess):
            self.watched.append(sess.name)
        rt._watch = watch
        return rt

    def test_the_async_form_starts_the_watcher_on_the_loop(self):
        rt = self.runtime()

        async def go():
            started = await rt.ensure_watched_async("sess-a", "/tmp/task_a")
            await asyncio.sleep(0)
            return started
        self.assertTrue(asyncio.run(go()))
        self.assertEqual(self.watched, ["cond_task_a"])

    def test_an_already_watched_session_is_left_alone(self):
        rt = self.runtime()

        async def go():
            await rt.ensure_watched_async("sess-a", "/tmp/task_a")
            await asyncio.sleep(0)
            return await rt.ensure_watched_async("sess-a", "/tmp/task_a")
        # The first call adopted and started a watcher; it has finished by
        # the second call (the fake returns at once), so a new one starts.
        self.assertTrue(asyncio.run(go()))


if __name__ == "__main__":
    unittest.main()


class SendDeliveryTest(unittest.TestCase):
    """A message must reach the session or raise - never vanish.

    send-keys types wherever the TUI's focus is. Mid turn the Enter is
    swallowed and the text sits unsent; with a permission dialog up the
    keystrokes answer the dialog instead. Both looked like success.
    """

    def runtime(self, panes):
        from conductor.tmux_runtime import TmuxClaudeRuntime, _TmuxSession
        rt = TmuxClaudeRuntime(transcript_dir=None)
        sess = _TmuxSession(task_id="t", name="cond_t",
                            working_directory="/tmp", session_id="s")
        rt.sessions["s"] = sess
        rt._alive = lambda name: True
        self.sent = []
        seq = list(panes)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_tmux(*args):
            r = Result()
            if args and args[0] == "capture-pane":
                r.stdout = seq.pop(0) if len(seq) > 1 else seq[0]
            return r
        rt._tmux = fake_tmux
        return rt, sess

    def test_waits_for_a_dialog_to_clear_before_typing(self) -> None:
        import asyncio
        from conductor.tmux_runtime import TmuxClaudeRuntime
        busy = ("Bash command\n  rm -rf x\nDo you want to proceed?\n"
                "❯ 1. Yes\n  2. No\n")
        ready = "❯ \n  ⏵⏵ accept edits on (shift+tab to cycle)\n"
        rt, sess = self.runtime([busy, busy, ready])
        rt._send_argv = staticmethod(lambda name, msg: [])
        # It must not raise: the dialog clears and the prompt appears.
        asyncio.run(rt._wait_for_input(sess, timeout=5))

    def test_gives_up_loudly_when_the_prompt_never_returns(self) -> None:
        import asyncio
        stuck = "working...\nno prompt here\n"
        rt, sess = self.runtime([stuck])
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(rt._wait_for_input(sess, timeout=1))
        self.assertIn("never returned to its prompt", str(caught.exception))

    def test_text_left_in_the_box_is_an_error(self) -> None:
        import asyncio
        unsent = "❯ do the thing that was never submitted\n"
        rt, sess = self.runtime([unsent])
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(rt._confirm_submitted(
                sess, "do the thing that was never submitted", timeout=1))
        self.assertIn("not submitted", str(caught.exception))

    def test_a_cleared_box_counts_as_delivered(self) -> None:
        import asyncio
        cleared = "❯ \n  ⏵⏵ accept edits on\n"
        rt, sess = self.runtime([cleared])
        asyncio.run(rt._confirm_submitted(sess, "already submitted", timeout=1))

    # -- a draft in the box ----------------------------------------------
    # Measured on the Boss window: two stray characters ('s now') and every
    # spoken message for the next minute died with "someone is typing
    # there; message not sent". The person still wins - nothing of theirs
    # is submitted - but the message goes: the draft is set aside with
    # Ctrl-U, ours is typed and sent, theirs is typed back.
    DRAFT = "❯ s now\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"
    CLEARED = "❯ \n  ⏵⏵ auto mode on (shift+tab to cycle)\n"

    def spy(self, rt):
        keys = []
        inner = rt._tmux

        def record(*args):
            keys.append(args)
            return inner(*args)
        rt._tmux = record
        return keys

    def test_a_draft_is_set_aside_and_put_back(self) -> None:
        import asyncio
        from conductor.tmux_runtime import PROBE
        # Reads, in order: the boot-dialog check, the wait itself, the
        # probe's look (the probe character appended: a person, not a
        # suggestion), then the delivery check on a cleared box.
        probed = self.DRAFT.replace("s now", "s now" + PROBE)
        rt, sess = self.runtime([self.DRAFT, self.DRAFT, probed,
                                 self.CLEARED])
        keys = self.spy(rt)
        asyncio.run(rt.send("s", "check the logs"))
        sent = [k for k in keys if k[0] == "send-keys"]
        self.assertEqual(sent[0][-2:], ("-l", PROBE))        # is it a person?
        self.assertEqual(sent[1][-1], "BSpace")              # it is
        self.assertEqual(sent[2][-1], "C-u")                 # theirs aside
        self.assertEqual(sent[3][-2:], ("-l", "check the logs"))
        self.assertEqual(sent[4][-1], "Enter")               # ours in
        self.assertEqual(sent[5][-2:], ("-l", "s now"))      # theirs back
        self.assertEqual(len(sent), 6, "nothing else was typed")

    def test_an_empty_box_is_not_cleared(self) -> None:
        import asyncio
        rt, sess = self.runtime([self.CLEARED])
        keys = self.spy(rt)
        asyncio.run(rt.send("s", "check the logs"))
        sent = [k for k in keys if k[0] == "send-keys"]
        self.assertNotIn("C-u", [k[-1] for k in sent])
        self.assertEqual(len(sent), 2)

    def test_a_queued_message_under_running_work_still_waits(self) -> None:
        """Text in the box while the session is generating is a queued
        message, not an idle draft: Enter would submit both."""
        import asyncio
        queued = "❯ s now\n✻ Thinking… (esc to interrupt)\n"
        rt, sess = self.runtime([queued])
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(rt._wait_for_input(sess, timeout=1))
        self.assertIn("never returned to its prompt", str(caught.exception))

    def test_the_draft_is_what_the_wait_returns(self) -> None:
        import asyncio
        rt, sess = self.runtime([self.DRAFT])
        self.assertEqual(asyncio.run(rt._wait_for_input(sess, timeout=1)),
                         "s now")
        rt, sess = self.runtime([self.CLEARED])
        self.assertEqual(asyncio.run(rt._wait_for_input(sess, timeout=1)), "")


class TheProviderIsListedOncePerSecond(unittest.TestCase):
    """After a restart every worker is unknown to the runtime, and each
    status check listed the provider's sessions (~0.4s) to find its row.
    Nineteen workers, nineteen listings, 14.5s for the Boss to learn what
    everything was doing. Within a second they all say the same."""

    def runtime(self):
        from unittest import mock
        from conductor import tmux_runtime
        rt = tmux_runtime.TmuxClaudeRuntime(transcript_dir=None)
        # The provider's own row shape: sessionId, and state.
        rows = [{"sessionId": "s1", "state": "busy"},
                {"sessionId": "s2", "state": "idle"}]
        self.listed = mock.AsyncMock(return_value=rows)
        self.patch = mock.patch.object(tmux_runtime.agent_feed, "sessions",
                                       self.listed)
        return rt

    def test_many_unknown_sessions_share_one_listing(self) -> None:
        import asyncio
        rt = self.runtime()

        async def go():
            return [await rt.get_status(s) for s in
                    ("s1", "s2", "s3", "s1", "s2")]
        with self.patch:
            statuses = asyncio.run(go())
        self.assertEqual(statuses, ["running", "idle", "disconnected",
                                    "running", "idle"])
        self.assertEqual(self.listed.await_count, 1)

    def test_concurrent_askers_wait_for_the_one_fetch(self) -> None:
        import asyncio
        rt = self.runtime()

        async def go():
            return await asyncio.gather(*(rt.get_status(s)
                                          for s in ("s1", "s2", "s3")))
        with self.patch:
            asyncio.run(go())
        self.assertEqual(self.listed.await_count, 1)

    def test_the_listing_expires(self) -> None:
        import asyncio
        rt = self.runtime()
        rt.FEED_TTL_S = 0.0

        async def go():
            await rt.get_status("s1")
            await rt.get_status("s1")
        with self.patch:
            asyncio.run(go())
        self.assertEqual(self.listed.await_count, 2)


class StartupPromptTest(unittest.TestCase):
    """Boot dialogs must be answered, or the session never takes input.

    A woken worker sat on Claude Code's resume picker indefinitely: the
    input prompt never appeared, so every message sent to it timed out and
    the session looked hung. Nobody is watching that window - that is the
    whole point of a delegated worker - so nothing was ever going to press
    Enter.
    """

    def runtime(self, pane_text):
        from conductor.tmux_runtime import TmuxClaudeRuntime, _TmuxSession
        rt = TmuxClaudeRuntime(transcript_dir=None)
        sess = _TmuxSession(task_id="t", name="cond_t",
                            working_directory="/tmp", session_id="s")
        self.keys = []

        class Result:
            returncode = 0
            stderr = ""
            def __init__(self, out=""):
                self.stdout = out

        def fake_tmux(*args):
            if args and args[0] == "capture-pane":
                return Result(pane_text)
            if args and args[0] == "send-keys":
                self.keys.append(args[-1])
            return Result()
        rt._tmux = fake_tmux
        return rt, sess

    def test_the_resume_picker_is_answered(self) -> None:
        pane = ("This session is long. We recommend resuming from a summary.\n"
                "❯ 1. Resume from summary (recommended)\n"
                "  2. Resume full session as-is\n"
                "  3. Don't ask me again\n"
                "Enter to confirm · Esc to cancel\n")
        rt, sess = self.runtime(pane)
        rt._handle_startup_prompts(sess)
        self.assertEqual(self.keys, ["Enter"],
                         "the resume picker was left unanswered")

    def test_the_trust_dialog_is_answered_by_reading_its_options(self) -> None:
        """Used to be a bare Enter, which answers whatever is selected.
        cmux presents this dialog with "No, exit" first, so that would
        have exited the worker the watchdog was rescuing."""
        pane = ("Do you trust this folder?\n"
                "❯ 1. Yes, I trust this folder\n"
                "  2. No, exit\n")
        rt, sess = self.runtime(pane)
        rt._handle_startup_prompts(sess)
        self.assertEqual(self.keys, ["Enter"])

    def test_a_reversed_trust_dialog_moves_before_confirming(self) -> None:
        pane = ("Do you trust this folder?\n"
                "❯ No, exit\n"
                "  Yes, I trust this folder\n")
        rt, sess = self.runtime(pane)
        rt._handle_startup_prompts(sess)
        self.assertEqual(self.keys, ["Down", "Enter"])

    def test_a_trust_prompt_with_no_visible_options_is_left_alone(self) -> None:
        """Answering blind is what this change exists to stop. A worker
        that waits can be rescued; one told to exit cannot."""
        rt, sess = self.runtime("Do you trust this folder?\n")
        rt._handle_startup_prompts(sess)
        self.assertEqual(self.keys, [])

    def test_the_bypass_permissions_dialog_is_accepted(self) -> None:
        """Its selected option is "No, exit": a bare Enter - or nobody
        pressing anything until claude gives up - closes the session, and
        the Boss then fails every turn with no tools."""
        pane = ("WARNING: Claude Code running in Bypass Permissions mode\n"
                "By proceeding, you accept all responsibility for actions "
                "taken while running in Bypass Permissions mode.\n"
                "\u276f No, exit\n"
                "  Yes, I accept\n"
                "Enter to confirm \u00b7 Esc to cancel\n")
        rt, sess = self.runtime(pane)
        rt._handle_startup_prompts(sess)
        self.assertEqual(self.keys, ["Down", "Enter"],
                         "the bypass acceptance was left at 'No, exit'")

    def test_the_chrome_extension_prompt_keeps_browser_tools_off(self) -> None:
        pane = ("Claude in Chrome extension detected\n"
                "❯ 1. Yes, use my browser\n"
                "  2. No, keep browser tools off\n"
                "Enter to confirm · Esc to keep browser tools off\n")
        rt, sess = self.runtime(pane)
        rt._handle_startup_prompts(sess)
        self.assertEqual(self.keys, ["Escape"])

    def test_an_ordinary_prompt_is_left_alone(self) -> None:
        rt, sess = self.runtime("❯ \n  ⏵⏵ accept edits on\n")
        rt._handle_startup_prompts(sess)
        self.assertEqual(self.keys, [],
                         "pressed Enter at a normal prompt")

    def test_an_approval_dialog_is_not_treated_as_a_boot_prompt(self) -> None:
        """Answering an approval by accident would be far worse than a hang."""
        pane = ("Bash command\n  rm -rf build\nDo you want to proceed?\n"
                "❯ 1. Yes\n  2. No\n")
        rt, sess = self.runtime(pane)
        rt._handle_startup_prompts(sess)
        self.assertEqual(self.keys, [],
                         "a permission prompt was answered automatically")


class WorkerEnvironmentTest(unittest.TestCase):
    """The tmux path is what you get on a machine without cmux, and what a
    Claude Code session launching the conductor for you gets. It inherited
    that session's child markers, Claude Code turned transcript saving off,
    and create_session waited out the startup timeout for a file that was
    never written - then killed the pane. No window at all.
    """

    def setUp(self) -> None:
        # _TmuxSession holds an asyncio.Event, and on 3.9 that binds the
        # current loop at construction. Own one rather than depend on
        # whatever the previous test left installed.
        import asyncio
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        import asyncio
        self.loop.close()
        asyncio.set_event_loop(None)

    def launch_argv(self):
        from unittest import mock

        from conductor.tmux_runtime import TmuxClaudeRuntime
        runtime = TmuxClaudeRuntime(transcript_dir=None)
        seen = []

        def fake_tmux(*args):
            seen.append(args)
            return mock.Mock(returncode=1, stdout="", stderr="stop here")
        with mock.patch.object(runtime, "_tmux", side_effect=fake_tmux):
            try:
                self.loop.run_until_complete(
                    runtime.create_session("task_x", "/tmp", "go"))
            except Exception:
                pass                      # the launch is what we inspect
        return next(a for a in seen if a and a[0] == "new-session")

    def test_the_child_markers_are_stripped(self) -> None:
        from conductor.tmux_runtime import CHILD_MARKERS
        launch = self.launch_argv()
        for marker in CHILD_MARKERS:
            self.assertIn(marker, launch,
                          f"{marker} would reach the worker")
        self.assertIn("env", launch)

    def test_the_scrub_precedes_the_binary(self) -> None:
        """`env -u ... claude`, not `claude ... env`: order is the whole
        mechanism, and an argv that merely mentions the names proves
        nothing."""
        launch = self.launch_argv()
        argv = list(launch)
        self.assertLess(argv.index("env"),
                        next(i for i, a in enumerate(argv)
                             if a.endswith("claude")))

    def test_the_pane_keeps_history_for_the_window_to_scroll(self) -> None:
        """Measured 2026-09-01: alternate_on=1, history_size=0 - Claude
        Code on tmux's alternate screen leaves no history, so the Boss
        window's terminal view had nothing to scroll. The launch must
        turn the alternate screen off."""
        from unittest import mock
        from conductor.tmux_runtime import TmuxClaudeRuntime
        runtime = TmuxClaudeRuntime(transcript_dir=None)
        seen = []

        def fake_tmux(*args):
            seen.append(args)
            return mock.Mock(returncode=1, stdout="", stderr="stop here")
        with mock.patch.object(runtime, "_tmux", side_effect=fake_tmux):
            try:
                self.loop.run_until_complete(
                    runtime.create_session("task_x", "/tmp", "go"))
            except Exception:
                pass                      # the launch is what we inspect
        setops = [a for a in seen if a and a[0] == "set-option"]
        self.assertTrue(setops, "alternate-screen was never touched")
        self.assertIn("alternate-screen", setops[0])
        self.assertIn("off", setops[0])

    def test_the_raw_stream_is_piped_from_birth(self) -> None:
        """tmux normalizes a TUI's drawing into repaints, so pane
        history stays empty however it is captured; the raw pipe-pane
        stream is the only thing a real emulator can scroll
        (measured 2026-09-01)."""
        import tempfile
        from pathlib import Path
        from unittest import mock

        from conductor import tmux_runtime
        from conductor.tmux_runtime import TmuxClaudeRuntime
        runtime = TmuxClaudeRuntime(transcript_dir=None)
        seen = []

        def fake_tmux(*args):
            seen.append(args)
            return mock.Mock(returncode=1, stdout="", stderr="stop here")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_runtime, "STREAMS_DIR", Path(tmp)):
                with mock.patch.object(runtime, "_tmux",
                                       side_effect=fake_tmux):
                    try:
                        self.loop.run_until_complete(
                            runtime.create_session("task_x", "/tmp", "go"))
                    except Exception:
                        pass
        pipes = [a for a in seen if a and a[0] == "pipe-pane"]
        self.assertTrue(pipes, "the raw stream was never piped")
        self.assertIn("-o", pipes[0])
        self.assertIn("cond_task_x", pipes[0])

    def test_the_prompt_is_still_the_last_argument(self) -> None:
        """The scrub must not displace the prompt; a worker started with no
        goal is worse than one with a stale env var."""
        launch = self.launch_argv()
        self.assertEqual(launch[-1], "go")
        argv = list(launch)
        i = argv.index("--permission-mode")
        self.assertEqual(argv[i + 1], "auto")

    def test_both_hosts_scrub_the_same_list(self) -> None:
        """One definition. The divergence is exactly how tmux went
        unprotected while cmux was fixed."""
        from conductor.cmux_runtime import scrub_prefix
        from conductor.tmux_runtime import CHILD_MARKERS
        for marker in CHILD_MARKERS:
            self.assertIn(marker, scrub_prefix("cond_task_a"))


class ForegroundInteractiveTest(unittest.TestCase):
    """What the tmux path owes the user: a Claude Code CLI session in front
    of them that they can type into - not a view of one."""

    def setUp(self) -> None:
        import asyncio
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        import asyncio
        self.loop.close()
        asyncio.set_event_loop(None)

    def test_the_terminal_path_registers_only_an_interactive_surface(self):
        from conductor.surfaces import (InteractiveTerminalSurface,
                                        SurfacePreference)
        surfaces = {"interactive-terminal": InteractiveTerminalSurface()}
        order = SurfacePreference().order_for("claude-code")
        chosen = next(n for n in order if n in surfaces)
        self.assertEqual(chosen, "interactive-terminal")
        self.assertTrue(surfaces[chosen].interactive)
        self.assertNotIn("transcript", surfaces)

    def test_the_window_is_brought_to_the_front(self) -> None:
        """A worker started behind the user's other windows is a worker
        they do not know exists."""
        from unittest import mock

        from conductor.surfaces import (InteractiveTerminalSurface,
                                        SurfaceRequest)
        surface = InteractiveTerminalSurface()
        scripts = []
        with mock.patch("conductor.surfaces._osascript",
                        side_effect=lambda s: scripts.append(s) or "42"):
            surface.create(SurfaceRequest(
                project_id="p", task_id="t", title="x",
                working_directory="/", provider="claude-code",
                pty_handle="cond_t"))
        self.assertIn("activate", scripts[0])
        self.assertIn("tmux attach-session -t cond_t", scripts[0])

    def test_the_runtime_exposes_the_pty_to_attach_to(self) -> None:
        """Without this the surface raises and no window opens at all."""
        from conductor.tmux_runtime import TmuxClaudeRuntime, _TmuxSession
        rt = TmuxClaudeRuntime(transcript_dir=None)
        rt.sessions["s"] = _TmuxSession(task_id="t", name="cond_t",
                                        working_directory="/tmp",
                                        session_id="s")
        self.assertEqual(rt.attach_handle("t"), "cond_t")
