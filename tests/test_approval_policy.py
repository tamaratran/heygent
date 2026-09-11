"""Which approvals the supervisor answers itself.

The policy had a safe list that never applied. Provider prompts wrap the
command - `Bash(git status)` - and the parser treated any segment beginning
with "bash" as boilerplate, so it discarded the command and classified the
human-readable description instead. Nothing matched, everything asked, and
the user was interrupted for `ls`.

Run with:  python3 -m unittest tests.test_approval_policy -v
"""

from __future__ import annotations

import asyncio
import unittest

from conductor.runtime import ApprovalPolicy, _bash_command


class CommandExtractionTest(unittest.TestCase):
    """Both provider wordings must yield the command itself."""

    def test_sdk_gist_form(self) -> None:
        self.assertEqual(
            _bash_command("Bash(git status --porcelain=v1 -uall) | Check the tree"),
            "git status --porcelain=v1 -uall")

    def test_tui_prompt_form(self) -> None:
        self.assertEqual(
            _bash_command('python3 -c "print(2+2)" | Run it | This command '
                          'requires approval'),
            'python3 -c "print(2+2)"')

    def test_tool_label_segment_is_not_the_command(self) -> None:
        """A third shape: the label is its own segment before the command."""
        self.assertEqual(
            _bash_command("Bash command | pytest -q | Run the test suite"),
            "pytest -q")

    def test_bare_command(self) -> None:
        self.assertEqual(_bash_command("$ ls -la"), "ls -la")

    def test_nested_parentheses_survive(self) -> None:
        self.assertEqual(_bash_command('Bash(python3 -c "print(6*7)")'),
                         'python3 -c "print(6*7)"')


class SupervisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ApprovalPolicy()

    def allow(self, text: str) -> None:
        self.assertEqual(self.policy.supervise(text, allow_write=True),
                         "allow", f"should not have interrupted: {text}")

    def ask(self, text: str) -> None:
        self.assertEqual(self.policy.supervise(text, allow_write=True),
                         "ask", f"should have asked: {text}")

    def test_routine_reads_never_interrupt(self) -> None:
        for text in ("Bash(git status --porcelain=v1 -uall) | Check the tree",
                     "Bash(ls -la) | List the directory",
                     "Bash(cat README.md) | Read the readme",
                     "Bash(git diff) | See the changes",
                     "Read(prompts/manager.md)",
                     "Grep(pattern) | Search the repo"):
            self.allow(text)

    def test_running_the_tests_is_routine(self) -> None:
        for text in ("Bash(pytest tests) | Run the tests",
                     "Bash(python3 -m unittest discover) | Run the suite"):
            self.allow(text)

    def test_consequential_actions_still_ask(self) -> None:
        for text in ("Bash(git push origin HEAD) | Push the branch",
                     "Bash(rm -rf build) | Clean up",
                     "Bash(npm install left-pad) | Add a dependency",
                     "Bash(curl https://example.com) | Fetch something",
                     "Bash(sudo rm /etc/hosts) | Fix DNS"):
            self.ask(text)

    def test_arbitrary_code_asks_even_though_it_looks_small(self) -> None:
        """`python3 -c` is not `python3 -m pytest`: it runs anything."""
        self.ask('python3 -c "print(2+2)" | Run python one-liner')

    def test_unrecognised_prompts_ask_rather_than_assume(self) -> None:
        self.ask("Something entirely unfamiliar happened")


class WorkerPermissionModeTest(unittest.TestCase):
    """Workers run unattended, so they start without permission gates.

    A delegated task has nobody watching its terminal; a gate there stops the
    work until somebody notices. The worktree bounds file changes - Bash does
    not - so this is a deliberate trade, and it is asserted rather than left
    to a default that could drift back.
    """

    def test_workers_start_in_auto(self) -> None:
        """Not bypass: ordinary work proceeds, and what the provider judges
        consequential is still weighed rather than waved through."""
        import boss
        self.assertEqual(boss.WORKER_PERMISSION_MODE, "auto")

    def test_there_is_no_read_only_worker_mode(self) -> None:
        """The fallback is gone, not merely defaulted off.

        It used to apply whenever --allow-write was absent, which is how a
        worker ended up prompting for every edit in a window nobody was
        watching - reported as "bypass is broken" rather than "a flag is
        missing"."""
        import boss
        self.assertFalse(hasattr(boss, "WORKER_PERMISSION_MODE_READ_ONLY"))

    def test_the_runtime_takes_no_read_only_switch(self) -> None:
        import inspect

        from conductor.tmux_runtime import TmuxClaudeRuntime
        params = inspect.signature(TmuxClaudeRuntime.__init__).parameters
        self.assertNotIn("allow_write", params)
        self.assertNotIn("read_only_mode", params)
        self.assertEqual(TmuxClaudeRuntime().permission_mode, "auto")

    def test_the_worker_is_launched_in_auto(self) -> None:
        """The invariant that matters: what actually reaches the argv."""
        from unittest import mock

        from conductor.tmux_runtime import TmuxClaudeRuntime
        runtime = TmuxClaudeRuntime()
        seen = []

        def fake_tmux(*args):
            seen.append(args)
            return mock.Mock(returncode=1, stdout="", stderr="stop here")
        with mock.patch.object(runtime, "_tmux", side_effect=fake_tmux):
            try:
                asyncio.run(runtime.create_session("task_x", "/tmp", "go"))
            except Exception:
                pass                      # the launch is what we inspect
        launch = next((a for a in seen if a and a[0] == "new-session"), None)
        self.assertIsNotNone(launch, "no session was launched")
        self.assertIn("--permission-mode", launch)
        self.assertEqual(launch[launch.index("--permission-mode") + 1],
                         "auto")

    def test_the_mode_is_one_the_cli_accepts(self) -> None:
        """A mode the binary rejects means no worker starts at all, which
        is worse than any prompting it was meant to avoid."""
        import boss
        self.assertIn(boss.WORKER_PERMISSION_MODE,
                      ("acceptEdits", "auto", "bypassPermissions",
                       "manual", "dontAsk", "plan"))

    def test_every_provider_has_a_bypass_equivalent(self) -> None:
        import boss
        for provider in ("claude-code", "codex"):
            self.assertIn(provider, boss.PROVIDER_BYPASS_FLAGS)
            self.assertTrue(boss.PROVIDER_BYPASS_FLAGS[provider])
