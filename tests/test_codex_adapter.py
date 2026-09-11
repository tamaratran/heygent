"""Codex's rollout is a transcript as good as Claude Code's.

Phase 3 of docs/any-cli.md. The lines below are the shapes measured in
a real rollout (2026-08-28, codex-cli 0.150) and the dialogs seen on
0.151.0: the adapter finds a checkout's rollout by session_meta.cwd,
names the session from the file name, and reads turns off
task_started / task_complete.

Run with:  python3 -m unittest tests.test_codex_adapter -v
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from conductor.cli_adapter import adapter_for
from conductor.codex_adapter import CodexAdapter
from conductor.tmux_runtime import choose_option

SID = "01a047da-53e7-74e3-83a8-17d71ffb526b"


def line(kind, **payload):
    return json.dumps({"timestamp": "2026-08-28T10:11:25.978Z", "ordinal": 0,
                       "type": kind, "payload": payload})


def rollout(cwd: str, sid: str = SID) -> list[str]:
    return [
        line("session_meta", id=sid, cwd=cwd, cli_version="0.151.0"),
        line("event_msg", type="task_started", turn_id="t1"),
        line("response_item", type="message", role="user",
             content=[{"type": "input_text", "text": "<recommended_plugins>\nAirtable"}]),
        line("response_item", type="message", role="user",
             content=[{"type": "input_text", "text": "What's the weather in SF?"}]),
        line("response_item", type="reasoning", summary=[]),
        line("response_item", type="message", role="assistant",
             content=[{"type": "output_text", "text": "I'm checking live conditions."}]),
        line("response_item", type="function_call", name="web_search",
             arguments={"query": "SF weather"}),
        line("event_msg", type="item_completed", item={"type": "web_search"}),
        line("event_msg", type="task_complete", turn_id="t1",
             last_agent_message="Sent the completed summary. No purchases were made."),
    ]


class TheRolloutIsTheTranscript(unittest.TestCase):
    def setUp(self):
        self.a = CodexAdapter("/usr/local/bin/codex")

    def test_turns_come_off_task_started_and_task_complete(self):
        state = {}
        seen = []
        for raw in rollout("/tmp/proj"):
            seen += [(e.type, e.summary, (e.detail or {}).get("source"),
                      (e.detail or {}).get("tool"))
                     for e in self.a.normalize(json.loads(raw), state)]
        self.assertEqual(seen[0][0], "started")
        users = [s for s in seen if s[2] == "user_message"]
        self.assertEqual(users, [("progress", "> What's the weather in SF?",
                                  "user_message", None)],
                         "Codex's injected <recommended_plugins> is not the user")
        self.assertIn(("progress", "I'm checking live conditions.", None, None), seen)
        self.assertIn(("progress", "web_search(SF weather)", None, "web_search"), seen)
        self.assertEqual(seen[-1][:2],
                         ("completed", "Sent the completed summary. No purchases were made."))

    def test_the_answer_falls_back_to_what_was_said(self):
        state = {}
        self.a.normalize(json.loads(line("response_item", type="message",
                                         role="assistant",
                                         content=[{"type": "output_text",
                                                   "text": "All done here."}])), state)
        (done,) = self.a.normalize(json.loads(line("event_msg", type="task_complete")),
                                   state)
        self.assertEqual((done.type, done.summary), ("completed", "All done here."))

    def test_launch_and_resume_lines(self):
        self.assertEqual(self.a.launch_argv("fix login", "auto"),
                         ["/usr/local/bin/codex", "-a", "on-request",
                          "-s", "workspace-write", "fix login"])
        self.assertEqual(self.a.launch_argv("x", "bypassPermissions"),
                         ["/usr/local/bin/codex",
                          "--dangerously-bypass-approvals-and-sandbox", "x"])
        self.assertEqual(self.a.resume_argv(SID),
                         ["/usr/local/bin/codex", "resume", SID])

    def test_the_dialogs_measured_on_0_151(self):
        trust = ("> You are in /tmp/gem\n  Do you trust the contents of this "
                 "directory? Working with untrusted contents\n› 1. Yes, continue\n"
                 "  2. No, quit\n  Press enter to continue\n")
        self.assertEqual(self.a.startup_dialog(trust), "trust")
        self.assertFalse(self.a.prompt_ready(trust))
        hooks = ("  Hooks need review\n  1 hook is new or changed.\n"
                 "› 1. Review hooks\n  2. Trust all and continue\n"
                 "  3. Continue without trusting (hooks won't run)\n")
        self.assertEqual(self.a.startup_dialog(hooks), "hooks")
        # The runtime answers it by moving to option 3.
        self.assertEqual(choose_option(hooks, "without trusting"), 2)
        self.assertEqual(choose_option(trust, "yes"), 0)


# The Codex worker's screen on 2026-08-30 (task_00eac099): the overlay
# as it asked to run `gh api user`, captured live; and the record it
# left after `gh pr list` was approved.
ASKING = """\
• I’ll run that exact read-only GitHub CLI check and make no changes.

• Running gh api user --jq .login


  Would you like to run the following command?

  Environment: local

  Reason: May I connect to GitHub to read the login for the currently
  authenticated account?

  $ gh api user --jq .login

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with `gh api
     user` (p)
  3. No, and tell Codex what to do differently (esc)

  Press enter to confirm or esc to cancel
"""
ANSWERED = """\
    … +2 lines (ctrl + t to view transcript)
    error connecting to api.github.com
    check your internet connection or https://githubstatus.com

✔ You approved codex to run gh pr list --repo tamaratran/voice-agent
  --state open --limit 100 this time

• Understood — listing the open PRs now.

› Ask Codex to do anything

  gpt-5 default · ~/.voice-conductor/workspaces/proj_19d43908/task…
"""


class TheApprovalOverlayIsKnownByItsShape(unittest.TestCase):
    """Measured 2026-08-30 09:23-09:25: "approve" matched the record
    Codex leaves once a prompt is answered ("✔ You approved codex to run
    ..."), so the prompt never "cleared" - delivery failure twice - and
    every send after it waited 45 s on a dialog that was not there."""

    def setUp(self):
        self.a = CodexAdapter("/usr/local/bin/codex")

    def test_the_overlay_is_an_approval_and_says_what_for(self):
        asked = self.a.approval_prompt(ASKING)
        self.assertIsNotNone(asked)
        self.assertIn("Would you like to run the following command?", asked)
        self.assertIn("$ gh api user --jq .login", asked, "the command, before the reason")
        self.assertIn("Reason: May I connect", asked)
        self.assertFalse(self.a.prompt_ready(ASKING), "a dialog owns the keyboard")

    def test_the_record_of_an_answered_one_is_not(self):
        self.assertIsNone(self.a.approval_prompt(ANSWERED))
        self.assertTrue(self.a.prompt_ready(ANSWERED))

    def test_the_other_questions_count_too(self):
        for question in ("Allow Codex to run `rm -rf build`?",
                         "Would you like to make the following edits?",
                         "Would you like to grant these permissions?"):
            screen = f"  {question}\n› 1. Yes, proceed\n  2. No, continue without running it\n"
            self.assertIsNotNone(self.a.approval_prompt(screen), question)
        # A question with no options under it is prose, not a prompt.
        self.assertIsNone(self.a.approval_prompt(
            "• I asked: would you like to run the following command? "
            "You said yes earlier.\n\n› Ask Codex to do anything\n"))

    def test_the_keys_are_codexs(self):
        self.assertEqual(self.a.approve_keys(ASKING), [["1"], ["Enter"]])
        self.assertEqual(self.a.deny_keys(ASKING), [["3"], ["Enter"]])
        # Nothing to pick from: fall back to Escape.
        self.assertEqual(self.a.deny_keys("  Allow Codex to run `x`?\n"), [["Escape"]])

    def test_claude_codes_keys_are_unchanged(self):
        from conductor.cli_adapter import ClaudeCodeAdapter
        c = ClaudeCodeAdapter("/usr/local/bin/claude")
        self.assertEqual(c.approve_keys(""), [["1"], ["Enter"]])
        self.assertEqual(c.deny_keys(""), [["Escape"]])


class ARolloutIsFoundByItsCheckout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = CodexAdapter("/usr/local/bin/codex")
        self.a.sessions_root = self.root
        day = self.root / "2026" / "08" / "30"
        day.mkdir(parents=True)
        self.mine = day / f"rollout-2026-08-30T01-00-00-{SID}.jsonl"
        self.mine.write_text("\n".join(rollout("/work/task_1")) + "\n")
        other = day / "rollout-2026-08-30T01-00-01-ffffffff-0000-0000-0000-000000000000.jsonl"
        other.write_text("\n".join(rollout("/work/task_2", "ffffffff-0000-0000-0000-000000000000")) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_this_checkouts_rollouts_are_candidates(self):
        found = self.a.transcripts("/work/task_1")
        self.assertEqual([p.name for p in found], [self.mine.name])
        self.assertEqual(self.a.session_id_of(self.mine), SID)
        self.assertEqual(self.a.transcript_for("/work/task_1", SID), self.mine)
        self.assertEqual(self.a.transcripts("/work/nowhere"), [])

    def test_old_rollouts_are_not_read(self):
        old = self.root / "2026" / "06" / "01"
        old.mkdir(parents=True)
        stale = old / "rollout-2026-06-01T00-00-00-11111111-0000-0000-0000-000000000000.jsonl"
        stale.write_text("\n".join(rollout("/work/task_1", "11111111-0000-0000-0000-000000000000")) + "\n")
        ancient = time.time() - 10 * 24 * 3600
        __import__("os").utime(stale, (ancient, ancient))
        self.assertEqual([p.name for p in self.a.transcripts("/work/task_1")],
                         [self.mine.name])

    def test_it_is_reachable_by_provider_name(self):
        self.assertIsInstance(adapter_for("codex"), CodexAdapter)


if __name__ == "__main__":
    unittest.main()
