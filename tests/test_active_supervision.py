"""Active supervision tests: an approval normally triggers a Manager-side
ACTION, not just a notification. Routine reads and searches auto-allow;
consequential gates escalate through the Manager's context; direct
resolution clears everything; delivery failure reconciles the same worker.

Run with:  python3 -m unittest tests.test_active_supervision -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.agent_events import AgentEvent
from conductor.global_conductor import GlobalConductor
from conductor.runtime import ApprovalPolicy
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


class PromptClassificationTest(unittest.TestCase):
    """The bug that blocked a repo search: every TUI prompt was classified
    as a Bash command. Prompt kinds now route correctly."""

    def setUp(self) -> None:
        self.policy = ApprovalPolicy()

    def test_repo_reads_and_searches_are_routine(self) -> None:
        for prompt in ("Search files | pattern: last-message timestamp",
                       "Read file | src/auth/login.ts",
                       "Grep | timestamp fields",
                       "List directory | conductor/",
                       "Search real repo for last-message timestamp fields"
                       " | Read"):
            self.assertEqual(
                self.policy.decide_prompt(prompt, allow_write=False),
                "allow", prompt)

    def test_workspace_edits_follow_write_policy(self) -> None:
        prompt = "Edit file | conductor/notifications.py"
        self.assertEqual(self.policy.decide_prompt(prompt, True), "allow")
        self.assertEqual(self.policy.decide_prompt(prompt, False), "ask")

    def test_bash_prompts_classify_by_command(self) -> None:
        self.assertEqual(self.policy.decide_prompt(
            "Bash command | pytest -q | Run the test suite", True), "allow")
        self.assertEqual(self.policy.decide_prompt(
            "Bash command | git push origin main | Push changes", True),
            "ask")
        self.assertEqual(self.policy.decide_prompt(
            "Bash command | some-unknown-binary --x", True), "ask")

    def test_unrecognized_prompts_never_assume(self) -> None:
        for prompt in ("Web search | how to bypass auth",
                       "Something entirely novel", ""):
            self.assertEqual(self.policy.decide_prompt(prompt, True), "ask",
                             prompt)


class SupervisionWorld(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = base / "code"
        (self.roots / "posely" / ".git").mkdir(parents=True)
        self.runtime = FakeCodingAgentRuntime()
        self.conductor = GlobalConductor(
            home=base / "home", runtime=self.runtime,
            search_roots=[self.roots],
            workspace_factory=lambda project: FakeWorkspaceManager())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def make_task(self, title: str = "Fix login"):
        registered = self.conductor.locator.register(self.roots / "posely")
        return self.run_async(self.conductor.create_task(
            title, "goal", project_id=registered.id))


class EscalationContextTest(SupervisionWorld):
    def test_pending_decision_leads_the_manager_context(self) -> None:
        task = self.make_task()
        self.runtime.emit_approval(task.provider_session_id, "appr_1",
                                   "Push these changes to GitHub")
        context = self.conductor.global_context()
        self.assertTrue(context.startswith("NEEDS YOUR DECISION:"))
        self.assertIn(task.id, context.splitlines()[1])
        self.assertIn("Push these changes to GitHub", context)
        self.assertIn("appr_1", context)

    def test_resolution_clears_the_escalation(self) -> None:
        task = self.make_task()
        self.runtime.emit_approval(task.provider_session_id, "appr_2",
                                   "risky thing")
        self.run_async(self.conductor.approve_task_action(task.id, "appr_2"))
        self.assertNotIn("NEEDS YOUR DECISION",
                         self.conductor.global_context())

    def test_direct_provider_resolution_stops_the_asking(self) -> None:
        """The user answered inside the session: the Manager must not keep
        asking about it."""
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit_approval(sid, "appr_3", "install package")
        self.assertIn("NEEDS YOUR DECISION", self.conductor.global_context())
        del self.runtime.pending[sid]["appr_3"]
        self.runtime.emit(sid, AgentEvent(
            type="approval_resolved",
            detail={"approval_id": "appr_3", "decision": "approved",
                    "resolved_by": "user_direct"}))
        self.assertNotIn("NEEDS YOUR DECISION",
                         self.conductor.global_context())

    def test_a_turn_ending_on_a_question_escalates_at_once(self) -> None:
        """A worker that stops to ask ends its turn: the runtime reports
        `completed`, never `needs_input`. Until the sweep found it (up to
        30 s later) it was in no escalation, and "anything waiting on me?"
        asked in between was answered no."""
        task = self.make_task()
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="completed",
            summary="I set up the badge. Badge only, or the full "
                    "CI workflow too?"))
        context = self.conductor.global_context()
        self.assertIn("NEEDS YOUR DECISION", context)
        self.assertIn("full CI workflow", context)
        self.run_async(self.conductor.send_to_task(task.id, "Badge only."))
        self.assertNotIn("NEEDS YOUR DECISION",
                         self.conductor.global_context())

    def test_a_plain_finish_does_not_escalate(self) -> None:
        task = self.make_task()
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="completed", summary="Added the badge to the README."))
        self.assertNotIn("NEEDS YOUR DECISION",
                         self.conductor.global_context())

    def test_input_questions_escalate_too(self) -> None:
        task = self.make_task()
        self.runtime.emit(task.provider_session_id, AgentEvent(
            type="needs_input",
            question="Should the migration stay backwards compatible?"))
        context = self.conductor.global_context()
        self.assertIn("NEEDS YOUR DECISION", context)
        self.assertIn("backwards compatible", context)
        # Answering the worker clears the escalation.
        self.run_async(self.conductor.send_to_task(task.id,
                                                   "Yes, keep it."))
        self.assertNotIn("NEEDS YOUR DECISION",
                         self.conductor.global_context())


class ApprovalSupervisorTest(SupervisionWorld):
    """The Manager takes responsibility for approvals: detect -> decide ->
    deliver into the SAME live session -> verify the worker advanced.
    Handled means demonstrably resumed, never just status+toast."""

    def test_leaked_routine_approval_is_auto_allowed_end_to_end(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        # A routine read leaked past the runtime's own policy (the original
        # repo-search bug): the supervisor resolves it instead of blocking.
        self.runtime.emit_approval(sid, "appr_1",
                                   "Search files | timestamp fields")
        actions = self.run_async(self.conductor.supervise_approvals())
        self.assertEqual(actions, [{"task_id": task.id,
                                    "approval_id": "appr_1",
                                    "decision": "allow"}])
        # Delivered to the SAME session and verified: pending cleared,
        # worker back to work, escalation gone, nothing replaced.
        self.assertIn(("approve", sid, "appr_1"), self.runtime.calls)
        self.assertEqual(self.run_async(
            self.conductor.subagent_for(task.id)).status, "working")
        self.assertNotIn("NEEDS YOUR DECISION",
                         self.conductor.global_context())
        self.assertEqual(self.runtime._counter, 1)

    def test_forbidden_action_is_auto_denied(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit_approval(sid, "appr_2",
                                   "Bash command | git push --force origin")
        actions = self.run_async(self.conductor.supervise_approvals())
        self.assertEqual(actions[0]["decision"], "deny")
        self.assertIn(("deny", sid, "appr_2"), self.runtime.calls)

    def test_consequential_approval_stays_escalated(self) -> None:
        task = self.make_task()
        self.runtime.emit_approval(task.provider_session_id, "appr_3",
                                   "Bash command | git push origin main")
        actions = self.run_async(self.conductor.supervise_approvals())
        self.assertEqual(actions, [])            # the user's call
        self.assertEqual(self.run_async(
            self.conductor.subagent_for(task.id)).status,
            "waiting_for_approval")
        self.assertIn("NEEDS YOUR DECISION", self.conductor.global_context())

    def test_failed_delivery_keeps_the_escalation(self) -> None:
        """An approval is not handled because a decision was sent: if the
        worker did not advance, the blocker remains the user's to see."""
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit_approval(sid, "appr_4", "Read file | src/x.ts")

        async def sticky_resolve(session_id, approval_id, approve):
            pass                       # decision "sent", gate never clears
        self.runtime.resolve_approval = sticky_resolve
        actions = self.run_async(self.conductor.supervise_approvals())
        self.assertEqual(actions, [])
        self.assertIn("NEEDS YOUR DECISION", self.conductor.global_context())
        state = {t.id: t for t in self.conductor.list_tasks()}[task.id]
        self.assertEqual(state.status, "waiting_for_user")   # not faked

    def test_supervision_verdicts(self) -> None:
        from conductor.runtime import ApprovalPolicy
        policy = ApprovalPolicy()
        self.assertEqual(policy.supervise("Search files | pattern"), "allow")
        self.assertEqual(policy.supervise(
            "Bash command | pytest -q"), "allow")
        self.assertEqual(policy.supervise(
            "Bash command | rm -rf / --no-preserve-root"), "deny")
        self.assertEqual(policy.supervise(
            "Bash command | git push --force origin main"), "deny")
        self.assertEqual(policy.supervise(
            "Bash command | git push origin main"), "ask")
        self.assertEqual(policy.supervise("Web search | anything"), "ask")


class NoReplacementTest(SupervisionWorld):
    def test_blocked_worker_is_never_replaced_by_supervision(self) -> None:
        task = self.make_task()
        self.runtime.emit_approval(task.provider_session_id, "appr_4",
                                   "risky")
        # Watchdog-style reconciliation while blocked: no new sessions.
        for _ in range(3):
            report = self.run_async(self.conductor.recover_task(task.id))
            self.assertEqual(report["action"], "none")
        # Other work proceeds while one worker is blocked: the Manager
        # loop is never held hostage.
        other = self.make_task("Other work")
        self.assertEqual(other.status, "running")
        self.assertEqual(self.runtime._counter, 2)


if __name__ == "__main__":
    unittest.main()
