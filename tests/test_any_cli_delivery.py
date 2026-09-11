"""Any CLI: delivery is the adapter's, and so is everything it is judged by.

The runtime checked that a follow-up had been submitted by finding the
input box - a line starting with Claude Code's "❯" or ">" - and seeing
the message leave it. Codex draws "›", Cursor "→", Gemini "│ >". For
them no box was ever found, and a missing box read as an empty one: a
follow-up still sitting in Codex's composer was reported sent, and a
person's draft there was never seen. "esc to interrupt" was the busy
mark, "1" and Enter the auto-approve keys, Enter the submit key - all
Claude Code's, for every CLI.

And after a restart the router placed every session with the default
runtime, Claude Code's, whatever CLI it was.

Here: each adapter says where its box is, what busy looks like and which
keys submit and approve; a CLI with a box nobody can read is judged by
its screen moving, and says so; a CLI described in providers.json runs
with no code at all; and a task's own provider routes its session.

Run with:  python3 -m unittest tests.test_any_cli_delivery -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import configured_adapter
from conductor.cli_adapter import ClaudeCodeAdapter, CliAdapter, adapter_for
from conductor.codex_adapter import CodexAdapter
from conductor.configured_adapter import ConfigError, ConfiguredAdapter
from conductor.conductor import Conductor
from conductor.routing_runtime import RoutingRuntime
from conductor.screen_adapter import CursorAdapter, GeminiAdapter
from conductor.task_types import Task
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager
from conductor.tmux_runtime import TmuxClaudeRuntime, _TmuxSession

MESSAGE = "run the gh commands and tell me which PRs are in flight"


class EachCliHasItsOwnBox(unittest.TestCase):
    def test_codex(self):
        codex = CodexAdapter("codex")
        screen = "• Done.\n\n› fix the failing\n  billing tests\n\n  gpt-5 default · ~/x\n"
        self.assertEqual(codex.input_box(screen), "fix the failing billing tests")
        self.assertEqual(codex.draft(screen), "fix the failing billing tests")
        empty = "• Done.\n\n› Ask Codex to do anything\n\n  gpt-5 default · ~/x\n"
        self.assertEqual(codex.draft(empty), "")

    def test_cursor_whose_footer_closes_the_box(self):
        cursor = CursorAdapter("cursor-agent")
        screen = "  → hello there\n  Auto · 8.3%\n  ~/gem\n"
        self.assertEqual(cursor.input_box(screen), "hello there")
        self.assertEqual(cursor.draft(
            "  → Plan, search, build anything\n  Auto · 8.3%\n"), "")
        self.assertEqual(cursor.draft("  → Add a follow-up\n  Auto · 8.3%\n"), "")

    def test_gemini_inside_its_frame(self):
        gemini = GeminiAdapter("gemini")
        screen = "╭──────────────╮\n│ > hello world │\n│   and more    │\n╰──────────────╯\n"
        self.assertEqual(gemini.input_box(screen), "hello world and more")
        self.assertEqual(gemini.draft(
            "╭────╮\n│ > Type your message or @path/to/file │\n╰────╯\n"), "")

    def test_claude_code_is_as_it_was(self):
        claude = ClaudeCodeAdapter("claude")
        screen = "❯\xa0also check the billing tests and then\n  the invoice export\n" \
                 + "─" * 20 + "\n  ⏵⏵ auto mode on\n"
        self.assertEqual(claude.draft(screen),
                         "also check the billing tests and then the invoice export")
        self.assertTrue(claude.busy("✻ Thinking… (esc to interrupt)"))

    def test_a_repls_submitted_line_is_history_not_the_box(self):
        """Measured live: a line REPL's prompt scrolls up with the text
        that was submitted still on it. Read as the box, the message
        looked unsent and its Enter was pressed a second time."""
        repl = ConfiguredAdapter("repl", {"command": ["repl"],
                                          "prompt_marks": ["> "]})
        self.assertIsNone(repl.input_box("repl 1.0\n> hello world\nthinking...\n"))
        self.assertEqual(repl.input_box("repl 1.0\n> hello world\n\n\n"),
                         "hello world")
        tui = ConfiguredAdapter("tui", {"command": ["tui"], "prompt_marks": ["> "],
                                        "chrome": "^model: .*"})
        self.assertEqual(tui.input_box("> draft\n  model: fast\n"), "draft")

    def test_a_cli_nobody_described_has_no_box_to_read(self):
        """None, not "": an unread box is not an empty one."""
        bare = CliAdapter("tool")
        self.assertIsNone(bare.input_box("> hello\n"))
        self.assertFalse(bare.reads_input_box)
        self.assertEqual(bare.draft("> hello\n"), "")


class Keyboard:
    """A pane that holds what is typed until the submit keys take."""

    def __init__(self, render, takes_on: int | None = 1,
                 submit=("Enter",)):
        self.render, self.takes_on, self.submit = render, takes_on, submit
        self.typed, self.presses, self.sent, self.keys = "", 0, [], []

    def __call__(self, *args):
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        if args[0] == "capture-pane":
            ok.stdout = self.render(self)
        elif args[0] == "send-keys":
            keys = list(args[args.index("-t") + 2:])
            self.keys.append(keys)
            if keys[0] == "-l":
                self.typed += keys[1]
            elif keys == ["C-u"]:
                self.typed = ""
            elif keys == ["BSpace"]:
                self.typed = self.typed[:-1]
            elif tuple(keys) == tuple(self.submit):
                self.presses += 1
                if self.takes_on is not None and self.presses >= self.takes_on \
                        and self.typed:
                    self.sent.append(self.typed)
                    self.typed = ""
        return ok


def runtime_for(adapter, keyboard) -> TmuxClaudeRuntime:
    rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
    rt.adapter = adapter
    rt.sessions = {"s": _TmuxSession(task_id="task_a", name="cond_task_a",
                                     working_directory="/tmp/task_a",
                                     session_id="s")}
    rt.startup_timeout = 1.0
    rt.CONFIRM_S = 0.3
    rt.CONFIRM_RETRY_S = 0.6
    rt.SEND_CHUNK_PAUSE_S = 0
    rt._tmux = keyboard
    return rt


def codex_screen(kb):
    return f"• Done.\n\n› {kb.typed or 'Ask Codex to do anything'}\n\n  gpt-5 default\n"


class AFollowUpLeftInTheBoxIsNotSent(unittest.TestCase):
    def test_codex_a_message_still_in_its_composer_is_an_error(self):
        """Before: the box was looked for by "❯", never found, and this
        send returned as delivered."""
        kb = Keyboard(codex_screen, takes_on=None)
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(runtime_for(CodexAdapter("codex"), kb).send("s", MESSAGE))
        self.assertIn("not submitted", str(caught.exception))
        self.assertEqual(kb.presses, 2)

    def test_codex_a_swallowed_enter_is_pressed_once_more(self):
        kb = Keyboard(codex_screen, takes_on=2)
        asyncio.run(runtime_for(CodexAdapter("codex"), kb).send("s", MESSAGE))
        self.assertEqual(kb.sent, [MESSAGE])
        self.assertEqual(kb.presses, 2)

    def test_cursor_a_draft_is_set_aside_and_restored(self):
        def screen(kb):
            return f"  → {kb.typed or 'Add a follow-up'}\n  Auto · 8.3%\n"
        kb = Keyboard(screen)
        kb.typed = "half a thought"
        asyncio.run(runtime_for(CursorAdapter("cursor-agent"), kb).send("s", MESSAGE))
        self.assertEqual(kb.sent, [MESSAGE])
        self.assertEqual(kb.typed, "half a thought")


class ABoxNobodyCanReadIsJudgedByTheScreen(unittest.TestCase):
    def adapter(self, **spec):
        return ConfiguredAdapter("tool", {"command": ["tool"], **spec})

    @staticmethod
    def repl(kb):
        history = "".join(f"> {line}\nok\n" for line in kb.sent)
        return f"tool 1.0\n{history}> {kb.typed}"

    def test_a_screen_that_moves_is_delivered(self):
        kb = Keyboard(self.repl)
        with mock.patch("conductor.tmux_runtime.application_log") as log:
            asyncio.run(runtime_for(self.adapter(), kb).send("s", MESSAGE))
        self.assertEqual(kb.sent, [MESSAGE])
        self.assertEqual(kb.presses, 1)
        self.assertNotIn("send.unverified", [c.args[1] for c in log.call_args_list])

    def test_a_screen_that_never_moves_is_unverified_not_sent(self):
        kb = Keyboard(self.repl, takes_on=None)
        with mock.patch("conductor.tmux_runtime.application_log") as log:
            asyncio.run(runtime_for(self.adapter(), kb).send("s", MESSAGE))
        self.assertEqual(kb.presses, 2)
        self.assertIn("send.unverified", [c.args[1] for c in log.call_args_list])

    def test_its_own_submit_keys_and_one_line(self):
        kb = Keyboard(self.repl, submit=("C-s",))
        adapter = self.adapter(submit=[["C-s"]])
        asyncio.run(runtime_for(adapter, kb).send("s", "first line\nsecond line"))
        self.assertEqual(kb.sent, ["first line second line"])
        self.assertNotIn(["Enter"], kb.keys)


class ApprovalsUseTheClisKeys(unittest.TestCase):
    def test_a_routine_approval_is_answered_with_the_adapters_keys(self):
        """It pressed "1" and Enter for every CLI."""
        adapter = ConfiguredAdapter("tool", {
            "command": ["tool"], "approval": r"Run it\? \(y/n\)",
            "approve_keys": [["y"]]})
        kb = Keyboard(lambda kb: "about to run: ls\nRun it? (y/n)\n")
        rt = runtime_for(adapter, kb)
        rt.approval_policy = mock.Mock()
        rt.approval_policy.decide_prompt.return_value = "allow"
        rt.bus = mock.Mock()
        rt._check_approval_prompt(rt.sessions["s"])
        self.assertEqual(kb.keys, [["y"]])


class AConfiguredCli(unittest.TestCase):
    def test_only_the_command_is_required(self):
        tool = ConfiguredAdapter("tool", {"command": "tool --quiet"})
        self.assertEqual(tool.launch_argv("the brief", "auto")[1:], ["--quiet"])
        self.assertFalse(tool.BRIEF_IN_ARGV)
        self.assertEqual(tool.resume_argv("scr_1")[1:], ["--quiet"])
        self.assertTrue(tool.prompt_ready("tool 1.0\n> "))
        self.assertFalse(tool.prompt_ready("   \n"))
        self.assertIsNone(tool.approval_prompt("Do you want to allow this?"),
                          "generic shapes would type approve keys into a CLI "
                          "that asked nothing")

    def test_the_brief_as_an_argument_or_a_flag(self):
        spec = {"command": ["aider"], "permission_flags": {"auto": ["--yes"]}}
        as_arg = ConfiguredAdapter("aider", {**spec, "brief": "argument"})
        self.assertEqual(as_arg.launch_argv("go", "auto")[1:], ["--yes", "go"])
        as_flag = ConfiguredAdapter("aider", {**spec, "brief": "--message"})
        self.assertEqual(as_flag.launch_argv("go", "auto")[1:],
                         ["--yes", "--message", "go"])
        self.assertTrue(as_flag.BRIEF_IN_ARGV)

    def test_a_turn_ends_on_quiet_and_not_while_busy(self):
        tool = ConfiguredAdapter("tool", {"command": ["tool"], "busy": "thinking",
                                          "settle_s": 0.6})
        state: dict = {}
        tool.screen_events("> ", state)
        tool.screen_events("> go\nthinking", state)
        for _ in range(4):
            self.assertEqual(tool.screen_events("> go\nthinking", state), [])
        tool.screen_events("> go\nthe answer\n> ", state)
        events = []
        for _ in range(3):
            events += tool.screen_events("> go\nthe answer\n> ", state)
        self.assertEqual([e.type for e in events], ["completed"])
        self.assertIn("the answer", events[0].summary)

    def test_busy_is_read_off_the_bottom_of_the_screen(self):
        """Measured live: "thinking..." stays in a REPL's history after the
        answer, and matched anywhere it kept the turn open for ever."""
        tool = ConfiguredAdapter("tool", {"command": ["tool"], "busy": "thinking"})
        self.assertTrue(tool.busy("> go\nthinking...\n"))
        self.assertFalse(tool.busy("> go\nthinking...\nanswer: a\nmore\n> \n"))
        prompted = ConfiguredAdapter("tool", {"command": ["tool"], "busy": "thinking",
                                              "prompt": r"^>\s*$"})
        self.assertFalse(prompted.busy("> go\nthinking...\nanswer: a\n>\n"))
        self.assertTrue(prompted.busy("> earlier\n> go\nthinking...\n"))
        boxed = ConfiguredAdapter("tool", {"command": ["tool"], "busy": "thinking",
                                           "prompt": r"^>\s*$", "prompt_marks": ["> "]})
        self.assertFalse(boxed.busy("thinking...\nanswer: a\n> half a draft\n"),
                         "a draft on the prompt line is not work")

    def test_an_answered_question_in_history_is_not_still_asking(self):
        tool = ConfiguredAdapter("tool", {"command": ["tool"], "approval": r"\(y/n\)"})
        self.assertIsNotNone(tool.approval_prompt("> delete it\nRun rm -rf build? (y/n)\n"))
        self.assertIsNone(tool.approval_prompt(
            "> delete it\nRun rm -rf build? (y/n) y\nran it\nthinking...\n"))

    def test_a_bad_entry_says_why(self):
        for spec, why in (({}, "command"), ({"command": ["t"], "prompt": "("}, "prompt"),
                          ({"command": ["t"], "brief": "stdin"}, "brief"),
                          ({"command": ["t"], "submit": "Enter"}, "submit")):
            with self.assertRaises(ConfigError) as caught:
                ConfiguredAdapter("t", spec)
            self.assertIn(why, str(caught.exception))


class TheProvidersFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(configured_adapter.CONFIGURED.clear)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        (self.home / "providers.json").write_text(json.dumps(data))

    def test_good_entries_load_and_bad_ones_are_reported(self):
        self.write({"providers": {
            "aider": {"command": ["aider"], "display": "Aider"},
            "Bad Name": {"command": ["x"]},
            "broken": {"command": ["b"], "busy": "("}}})
        good, problems = configured_adapter.load(self.home)
        self.assertEqual(list(good), ["aider"])
        self.assertEqual(len(problems), 2)
        adapter = adapter_for("aider", "/bin/aider")
        self.assertIsInstance(adapter, ConfiguredAdapter)
        self.assertEqual(adapter.display, "Aider")

    def test_no_file_is_no_providers(self):
        self.assertEqual(configured_adapter.load(self.home), ({}, []))

    def test_a_built_in_name_stays_built_in(self):
        self.write({"gemini": {"command": ["my-gemini"]}})
        configured_adapter.load(self.home)
        self.assertIsInstance(adapter_for("gemini", "/bin/gemini"), GeminiAdapter)

    def test_the_task_and_the_capability_list_know_it(self):
        from conductor.capabilities import snapshot
        self.write({"aider": {"command": ["aider"], "display": "Aider"}})
        configured_adapter.load(self.home)
        Task(id="task_x", title="t", goal="g", provider="aider")
        conductor = mock.Mock()
        tool_runtime = FakeCodingAgentRuntime()
        tool_runtime.provider = "aider"
        conductor.runtime = RoutingRuntime(local=FakeCodingAgentRuntime(),
                                           providers={"aider": tool_runtime})
        conductor.locator, conductor.surfaces = None, {}
        with mock.patch("conductor.capabilities.shutil.which",
                        side_effect=lambda b: f"/bin/{b}"):
            state = snapshot(conductor)
        self.assertEqual(state['Aider (provider "aider")'], (True, ""))

    def test_an_unknown_provider_is_still_refused(self):
        tmp = self.home / "repo"
        (tmp / ".git").mkdir(parents=True)
        conductor = Conductor(tmp, runtime=FakeCodingAgentRuntime(),
                              workspaces=FakeWorkspaceManager())
        with self.assertRaises(ValueError):
            asyncio.run(conductor.create_task("x", "goal", provider="aider"))


class TheBriefIsTypedWhenTheCliTakesNone(unittest.TestCase):
    def test_create_session_types_it_once_the_screen_settles(self):
        rt = TmuxClaudeRuntime.__new__(TmuxClaudeRuntime)
        rt.sessions = {}
        rt.adapter = ConfiguredAdapter("tool", {"command": ["tool"]})
        rt.permission_mode = "auto"
        order = []

        async def launch(task_id, cwd, argv, **kw):
            order.append(("launch", argv[1:]))
            rt.sessions["scr_1"] = _TmuxSession(task_id=task_id, name="n",
                                                working_directory=cwd,
                                                session_id="scr_1")
            return "scr_1"

        async def quiet(sess):
            order.append(("quiet",))

        async def send(sid, text):
            order.append(("send", sid, text))
        rt.launch_session, rt._wait_quiet, rt.send = launch, quiet, send
        sid = asyncio.run(rt.create_session("task_a", "/tmp/a", "the brief"))
        self.assertEqual(sid, "scr_1")
        self.assertEqual(order, [("launch", []), ("quiet",),
                                 ("send", "scr_1", "the brief")])


class Recording(FakeCodingAgentRuntime):
    def __init__(self, label):
        super().__init__()
        self.label = label


class ARestartRoutesByTheTasksProvider(unittest.TestCase):
    """After a restart the router has no owners; a Gemini task's follow-up
    went to Claude Code's runtime."""

    def test_a_follow_up_reaches_the_clis_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            local, gemini = Recording("local"), Recording("gemini")
            first = Conductor(root, runtime=RoutingRuntime(
                local=local, providers={"gemini": gemini}),
                workspaces=FakeWorkspaceManager())
            task = asyncio.run(first.create_task("t", "goal", provider="gemini"))
            sid = task.provider_session_id

            # The restart: a new router, the same store, a live worker.
            local2, gemini2 = Recording("local"), Recording("gemini")
            gemini2.statuses[sid] = "idle"
            router = RoutingRuntime(local=local2, providers={"gemini": gemini2})
            second = Conductor(root, runtime=router,
                               workspaces=FakeWorkspaceManager(),
                               store=first.store)
            asyncio.run(second.send_to_task(task.id, "and the docs"))
            self.assertIn(("send", sid, "and the docs"), gemini2.calls)
            self.assertFalse([c for c in local2.calls if c[0] in ("send", "resume")],
                             "Claude Code's runtime was asked to resume a "
                             "Gemini session")

    def test_a_placed_session_is_never_moved(self):
        router = RoutingRuntime(local=Recording("local"),
                                providers={"gemini": Recording("gemini")})
        router.owner["s1"] = "local"
        router.route("s1", "gemini")
        router.route("s2", "nonesuch")
        self.assertEqual(router.owner, {"s1": "local"})


if __name__ == "__main__":
    unittest.main()
