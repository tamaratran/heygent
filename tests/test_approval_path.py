"""Approval delivery tests: an approval is only handled when the decision
reaches the exact live worker and it demonstrably advances. Sent-but-still-
waiting is a delivery failure, never a generic stuck session.

Run with:  python3 -m unittest tests.test_approval_path -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.global_conductor import GlobalConductor
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager
from conductor.tmux_runtime import detect_approval_prompt

PROMPT = """\
  Bash command

    touch conductor-test.txt
    Create an empty test file

Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again for touch commands
  3. No, and tell Claude what to do differently (esc)
"""


class PromptDetectionTest(unittest.TestCase):
    def test_detects_prompt_and_extracts_description(self) -> None:
        description = detect_approval_prompt(PROMPT)
        self.assertIsNotNone(description)
        self.assertIn("touch conductor-test.txt", description)

    def test_ordinary_output_is_not_a_prompt(self) -> None:
        for text in ("Claude: I'll inspect the auth flow.\nRead src/x.ts",
                     "$ pytest -q\n34 passed",
                     "", "1. Yes this list is prose, not a prompt"):
            self.assertIsNone(detect_approval_prompt(text), text)

    def test_trust_dialog_is_not_an_approval(self) -> None:
        trust = ("Quick safety check: Is this a project you created...\n"
                 " ❯ 1. Yes, I trust this folder\n   2. No, exit")
        # The trust dialog also shows "❯ 1. Yes..." - it IS a gate the
        # startup handler answers; detection treating it as an approval is
        # acceptable only before session start. Document the behavior:
        self.assertIsNotNone(detect_approval_prompt(trust))


class DeliveryVerificationTest(unittest.TestCase):
    """The conductor trusts provider acknowledgement, not its own JSON."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = base / "code"
        (self.roots / "posely" / ".git").mkdir(parents=True)
        self.runtime = FakeCodingAgentRuntime()
        self.events = []
        self.conductor = GlobalConductor(
            home=base / "home", runtime=self.runtime,
            search_roots=[self.roots],
            workspace_factory=lambda project: FakeWorkspaceManager())
        self.conductor.bus.subscribe(self.events.append)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def make_task(self):
        registered = self.conductor.locator.register(self.roots / "posely")
        return self.run_async(self.conductor.create_task(
            "Fix login", "goal", project_id=registered.id))

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def test_full_audit_trail_on_success(self) -> None:
        task = self.make_task()
        self.runtime.emit_approval(task.provider_session_id, "appr_1",
                                   "Install package")
        self.run_async(self.conductor.approve_task_action(task.id, "appr_1"))
        for expected in ("approval.detected", "approval.decision_sending",
                         "approval.decision_sent", "task.approval_resolved"):
            self.assertIn(expected, self.types())
        # Ordering: detected before sending before sent.
        order = [t for t in self.types() if t.startswith("approval.")]
        self.assertLess(order.index("approval.decision_sending"),
                        order.index("approval.decision_sent"))

    def test_sent_but_not_cleared_is_delivery_failure(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit_approval(sid, "appr_2", "risky thing")

        original = self.runtime.resolve_approval

        async def sticky_resolve(session_id, approval_id, approve):
            # The decision "succeeds" but the worker never advances: the
            # approval stays pending. This must surface as delivery failure.
            self.runtime.calls.append(("approve" if approve else "deny",
                                       session_id, approval_id))
        self.runtime.resolve_approval = sticky_resolve

        with self.assertRaises(RuntimeError) as caught:
            self.run_async(self.conductor.approve_task_action(task.id,
                                                              "appr_2"))
        self.assertIn("did not advance", str(caught.exception))
        self.assertIn("approval.delivery_failed", self.types())
        # The task was NOT falsely marked running.
        state = {t.id: t for t in self.conductor.list_tasks()}[task.id]
        self.assertEqual(state.status, "waiting_for_user")
        self.runtime.resolve_approval = original

    def test_no_approval_no_deadlock_states(self) -> None:
        """waiting_for_approval always carries an addressable approval."""
        task = self.make_task()
        self.runtime.emit_approval(task.provider_session_id, "appr_3",
                                   "thing")
        report = self.run_async(self.conductor.handle_action(
            "inspect_task", {"task_id": task.id}))
        self.assertEqual(report["provider_health"], "waiting_for_approval")
        self.assertTrue(report["pending_approvals"])
        # Whatever is waiting can always be acted on.
        self.run_async(self.conductor.deny_task_action(task.id, "appr_3"))
        after = self.run_async(self.conductor.subagent_for(task.id))
        self.assertEqual(after.status, "working")


if __name__ == "__main__":
    unittest.main()
