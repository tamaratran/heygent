"""boss-mcp as shipped tooling: a helper on a runtime we control, verified
before any Boss is launched, configured for that one session and no
other.

Run with:  python3 -m unittest tests.test_boss_tooling -v
"""

from __future__ import annotations

import inspect
import re
import tempfile
import unittest
from pathlib import Path

from conductor import boss_helper
from conductor.boss_helper import (HelperCheck, ensure_helper, launcher_text,
                                   verify_helper)
from conductor.boss_tools import (DESCRIPTIONS, PROTOCOL_VERSION,
                                  REQUIRED_TOOLS, SCHEMAS, TITLE_MAX_CHARS,
                                  check_title)


class TheHelperRunsOnARuntimeWeControl(unittest.TestCase):
    def test_the_launcher_never_uses_the_system_python(self):
        text = launcher_text("/Users/x/.local/bin/uv", Path("/repo"))
        self.assertIn('"/Users/x/.local/bin/uv" run', text)
        self.assertIn("--python-preference only-managed", text)
        self.assertIn("--with \"mcp>=2,<3\"", text)
        self.assertNotIn("/usr/bin/python", text)
        self.assertNotIn("python3 ", text.split("run", 1)[0])

    def test_the_launcher_pins_our_code_by_absolute_path(self):
        text = launcher_text("/uv", Path("/repo"))
        self.assertIn("sys.path.insert(0, '/repo')", text)
        self.assertIn("from conductor.boss_mcp import main", text)

    def test_ensure_helper_writes_an_executable_at_an_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ensure_helper(tmp, "/repo", uv="/uv")
            self.assertTrue(path.is_absolute())
            self.assertEqual(path.name, "boss-mcp")
            self.assertTrue(path.stat().st_mode & 0o111)
            again = ensure_helper(tmp, "/repo", uv="/uv")
            self.assertEqual(path, again)

    def test_without_uv_it_says_so_rather_than_guessing(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(boss_helper, "find_uv", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                ensure_helper(tmp, "/repo")
        self.assertIn("uv is not installed", str(caught.exception))


def fake(directory: Path, body: str) -> Path:
    path = directory / "helper"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class VerifyingBeforeLaunching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_matching_helper_passes(self):
        path = fake(self.dir, f'echo "boss-mcp protocol {PROTOCOL_VERSION}"\n')
        check = verify_helper(path)
        self.assertTrue(check.ok)
        self.assertEqual(check.protocol, PROTOCOL_VERSION)

    def test_a_mismatched_protocol_is_incompatible_not_partially_run(self):
        path = fake(self.dir, f'echo "boss-mcp protocol {PROTOCOL_VERSION + 5}"\n')
        check = verify_helper(path)
        self.assertFalse(check.ok)
        self.assertIn("incompatible", check.problem)

    def test_a_helper_that_cannot_start_fails_clearly(self):
        path = fake(self.dir, 'echo "boom" >&2; exit 1\n')
        check = verify_helper(path)
        self.assertFalse(check.ok)
        self.assertIn("boom", check.problem)

    def test_a_missing_helper_fails_clearly(self):
        check = verify_helper(self.dir / "nope")
        self.assertFalse(check.ok)
        self.assertIn("not executable", check.problem)

    def test_a_helper_with_no_version_line_fails(self):
        path = fake(self.dir, 'echo "hello"\n')
        self.assertFalse(verify_helper(path).ok)


class ConfigurationIsScopedToOurBoss(unittest.TestCase):
    """An unrelated Claude Code session the user opens must not gain
    spawn_subagent. Our config is a file named on OUR session's command
    line and nothing else."""

    SOURCES = ("conductor/pty_manager.py", "conductor/boss_helper.py",
               "conductor/boss_mcp.py", "conductor/boss_bridge.py", "conduct.py",
               "conductor/cli_adapter.py", "conductor/codex_adapter.py",
               "conductor/screen_adapter.py")

    def test_nothing_touches_the_users_global_claude_config(self):
        for name in self.SOURCES:
            text = Path(name).read_text()
            self.assertNotIn(".claude.json", text, name)
            self.assertNotIn("claude mcp add", text, name)
            self.assertNotRegex(text, r"\.claude/settings", name)

    def test_the_config_is_written_under_our_home_and_named_explicitly(self):
        from conductor.cli_adapter import ClaudeCodeAdapter
        source = inspect.getsource(ClaudeCodeAdapter.boss_config_path)
        self.assertIn('boss_dir / "mcp.json"', source)
        argv = inspect.getsource(ClaudeCodeAdapter.boss_argv)
        self.assertIn('"--mcp-config"', argv)
        self.assertIn('"--strict-mcp-config"', argv)


class OneDefinitionThreeConsumers(unittest.TestCase):
    def test_the_sdk_backend_reads_the_same_definitions(self):
        from conductor import claude_manager
        self.assertIs(claude_manager._SCHEMAS, SCHEMAS)
        self.assertIs(claude_manager._DESCRIPTIONS, DESCRIPTIONS)

    def test_every_tool_is_described_and_required_tools_exist(self):
        self.assertEqual(set(SCHEMAS), set(DESCRIPTIONS))
        for name in REQUIRED_TOOLS:
            self.assertIn(name, SCHEMAS)

    def test_the_protocol_version_is_what_the_helper_reports(self):
        source = inspect.getsource(boss_helper.verify_helper)
        self.assertIn("PROTOCOL_VERSION", source)


class AnOptionalParameterIsOptionalEverywhere(unittest.TestCase):
    """create_task predates provider; a call that leaves it out is still a
    valid call, whichever of the three consumers derives the schema."""

    def test_boss_mcp_leaves_provider_out_of_the_required_list(self):
        from conductor.boss_mcp import _handler
        handler = _handler("create_task", SCHEMAS["create_task"],
                           "/tmp/sock", "token", "boss")
        params = inspect.signature(handler).parameters
        self.assertIsNot(params["provider"].default, inspect.Parameter.empty)
        self.assertIs(params["title"].default, inspect.Parameter.empty)

    def test_conductor_mcp_leaves_provider_out_of_the_required_list(self):
        from conductor.conductor_mcp import ConductorMcp
        mcp = ConductorMcp.__new__(ConductorMcp)
        handler = mcp._handler("create_task", SCHEMAS["create_task"])
        params = inspect.signature(handler).parameters
        self.assertIsNot(params["provider"].default, inspect.Parameter.empty)
        self.assertIs(params["goal"].default, inspect.Parameter.empty)

    def test_the_sdk_backend_leaves_provider_out_of_the_required_list(self):
        from conductor.claude_manager import _sdk_schema
        schema = _sdk_schema("create_task")
        self.assertNotIn("provider", schema["required"])
        self.assertIn("title", schema["required"])
        self.assertIn("provider", schema["properties"])
        self.assertEqual(_sdk_schema("send_to_task"),
                         SCHEMAS["send_to_task"])


class TitlesFitWhereTheyAreRead(unittest.TestCase):
    """A title is capped when the Boss writes it, so nothing downstream has
    to cut it and add an ellipsis."""

    def test_the_tool_and_the_prompt_state_the_same_limit(self):
        self.assertIn(f"{TITLE_MAX_CHARS} characters", DESCRIPTIONS["create_task"])
        prompt = (Path(__file__).resolve().parent.parent
                  / "prompts/manager.md").read_text()
        self.assertIn(f"{TITLE_MAX_CHARS} characters", prompt)

    def test_a_title_within_the_limit_is_kept_whole(self):
        title = "x" * TITLE_MAX_CHARS
        self.assertEqual(check_title(title), title)
        self.assertEqual(check_title("  Fix   login\n"), "Fix login")

    def test_a_longer_title_is_cut_at_a_word_not_refused(self):
        """No error round-trip for a title: the Boss was told the limit,
        and if it overshoots the user gets the first words, whole."""
        long = "Fix transcript spacing around punctuation and noise tags"
        self.assertEqual(check_title(long), "Fix transcript spacing around")
        self.assertLessEqual(len(check_title(long)), TITLE_MAX_CHARS)

    def test_the_cut_never_ends_on_a_dangling_mark(self):
        self.assertEqual(check_title("Fix the login redirect loop on Safari, " "then iOS"),
                         "Fix the login redirect loop on Safari")

    def test_no_ellipsis_is_added(self):
        self.assertNotIn("…", check_title("y " * 40))
        self.assertNotIn("...", check_title("y " * 40))

    def test_one_unbroken_word_is_cut_hard(self):
        self.assertEqual(check_title("y" * 50), "y" * TITLE_MAX_CHARS)


class AnEmptyTitleIsRefusedToo(unittest.TestCase):
    def test_blank_is_not_a_title(self):
        from conductor.boss_tools import check_title
        for blank in ("", "   ", None):
            with self.assertRaises(ValueError) as caught:
                check_title(blank)
            self.assertIn("needs a title", str(caught.exception))

    def test_whitespace_is_folded_before_counting(self):
        from conductor.boss_tools import check_title, TITLE_MAX_CHARS
        self.assertEqual(check_title("  Fix   login\nredirect  "), "Fix login redirect")
        self.assertEqual(len(check_title(" ".join(["ab"] * 13) + " x")), TITLE_MAX_CHARS)


if __name__ == "__main__":
    unittest.main()
