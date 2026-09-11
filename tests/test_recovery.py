"""Session health, recovery, approvals, and terminal-reuse tests - the
spec's section 46 acceptance cases, all deterministic.

The rule under test: a task that appears stuck is first understood, never
duplicated. Reuse -> resume -> reconstruct, in that order, single-flight.

Run with:  python3 -m unittest tests.test_recovery -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.agent_events import AgentEvent
from conductor.runtime import ApprovalPolicy
from conductor.global_conductor import GlobalConductor
from conductor.surfaces import FakeSurface, SurfaceHandle
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


class RecoveryWorld(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = base / "code"
        (self.roots / "posely" / ".git").mkdir(parents=True)
        self.runtime = FakeCodingAgentRuntime()
        self.surface = FakeSurface()
        self.conductor = GlobalConductor(
            home=base / "home", runtime=self.runtime,
            search_roots=[self.roots], surface=self.surface,
            workspace_factory=lambda project: FakeWorkspaceManager())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def make_task(self, title: str = "Fix login"):
        registered = self.conductor.locator.register(self.roots / "posely")
        return self.run_async(self.conductor.create_task(
            title, "goal", project_id=registered.id))


class TerminalReuseTest(RecoveryWorld):
    def test_a_follow_ups_never_spawn_windows(self) -> None:
        task = self.make_task()
        for i in range(10):
            self.run_async(self.conductor.send_to_task(task.id, f"m{i}"))
        self.assertEqual(len(self.surface.created), 1)
        sends = [c for c in self.runtime.calls if c[0] == "send"]
        self.assertEqual({c[1] for c in sends}, {task.provider_session_id})

    def test_b_c_healthy_or_waiting_session_is_left_alone(self) -> None:
        task = self.make_task()
        for status in ("running", "idle"):
            self.runtime.statuses[task.provider_session_id] = status
            report = self.run_async(self.conductor.recover_task(task.id))
            self.assertEqual(report["action"], "none")
        self.assertEqual(len(self.surface.created), 1)
        self.assertEqual(self.runtime._counter, 1)   # one session, ever

    def test_focus_reuses_available_surface(self) -> None:
        task = self.make_task()
        for _ in range(3):
            self.run_async(self.conductor.focus_task(task.id))
        self.assertEqual(len(self.surface.created), 1)
        self.assertEqual(len(self.surface.focused), 3)


class ApprovalTest(RecoveryWorld):
    def test_d_f_approval_pauses_then_same_session_resumes(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.emit_approval(sid, "appr_1", "Install package foo")
        # Task waits; nothing was replaced.
        state = {t.id: t for t in self.conductor.list_tasks()}[task.id]
        self.assertEqual(state.status, "waiting_for_user")
        self.assertEqual(len(self.surface.created), 1)
        # The Manager can see why through canonical state.
        report = self.run_async(self.conductor.handle_action(
            "inspect_task", {"task_id": task.id}))
        self.assertEqual(report["provider_health"], "waiting_for_approval")
        self.assertEqual(report["pending_approvals"][0]["approval_id"],
                         "appr_1")
        # The user says yes; the SAME session continues.
        self.run_async(self.conductor.handle_action(
            "approve_task_action", {"task_id": task.id,
                                    "approval_id": "appr_1"}))
        self.assertIn(("approve", sid, "appr_1"), self.runtime.calls)
        state = {t.id: t for t in self.conductor.list_tasks()}[task.id]
        self.assertEqual(state.status, "running")
        self.assertEqual(state.provider_session_id, sid)
        self.assertEqual(len(self.surface.created), 1)

    def test_deny_and_idempotency(self) -> None:
        task = self.make_task()
        self.runtime.emit_approval(task.provider_session_id, "appr_2",
                                   "git push")
        self.run_async(self.conductor.deny_task_action(task.id, "appr_2"))
        # A second answer to the same approval is refused, never re-sent.
        with self.assertRaises(KeyError):
            self.run_async(self.conductor.approve_task_action(task.id,
                                                              "appr_2"))
        with self.assertRaises(KeyError):
            self.run_async(self.conductor.approve_task_action(
                task.id, "appr_never_existed"))

    def test_approval_policy_classification(self) -> None:
        policy = ApprovalPolicy()
        self.assertEqual(policy.decide("Read", {"file_path": "x"}), "allow")
        self.assertEqual(policy.decide("Edit", {"file_path": "x"}), "allow")
        self.assertEqual(policy.decide("Bash", {"command": "pytest -q"}),
                         "allow")
        self.assertEqual(policy.decide("Bash", {"command": "git status"}),
                         "allow")
        for risky in ("rm -rf build", "git push origin main",
                      "pip install left-pad", "curl http://evil",
                      "some-unknown-binary --flag"):
            self.assertEqual(policy.decide("Bash", {"command": risky}),
                             "ask", risky)


class RecoveryTest(RecoveryWorld):
    def test_g_unreachable_resumes_no_duplicates(self) -> None:
        task = self.make_task()
        sid = task.provider_session_id
        self.runtime.vanish(sid)
        report = self.run_async(self.conductor.recover_task(task.id))
        self.assertEqual(report["action"], "resumed")
        self.assertIn(("resume", sid, task.workspace.path),
                      self.runtime.calls)
        self.assertEqual(self.runtime._counter, 1)     # no new session
        self.assertEqual(len(self.surface.created), 1)  # no new terminal

    def test_h_confirmed_missing_reconstructs_reusing_surface(self) -> None:
        task = self.make_task()
        old_sid = task.provider_session_id
        self.runtime.vanish(old_sid)
        self.runtime.fail_resume = True                # positively gone
        report = self.run_async(self.conductor.recover_task(task.id))
        self.assertEqual(report["action"], "reconstructed")
        refreshed = {t.id: t for t in self.conductor.list_tasks()}[task.id]
        self.assertEqual(refreshed.id, task.id)        # identity survives
        self.assertNotEqual(refreshed.provider_session_id, old_sid)
        self.assertEqual(refreshed.status, "running")
        # The replacement was briefed from durable context, and the still-
        # open terminal was reused: exactly one surface, ever.
        replacement = [c for c in self.runtime.calls
                       if c[0] == "create"][-1]
        self.assertIn("replacing a previous worker", replacement[3])
        self.assertEqual(len(self.surface.created), 1)

    def test_i_missing_session_and_surface_recreates_both_once(self) -> None:
        task = self.make_task()
        self.runtime.vanish(task.provider_session_id)
        self.runtime.fail_resume = True
        self.surface.close(SurfaceHandle.from_dict(task.surface))
        report = self.run_async(self.conductor.recover_task(task.id))
        self.assertEqual(report["action"], "reconstructed")
        self.assertEqual(len(self.surface.created), 2)   # exactly one more
        refreshed = {t.id: t for t in self.conductor.list_tasks()}[task.id]
        self.assertEqual(refreshed.id, task.id)

    def test_j_concurrent_triggers_share_one_recovery(self) -> None:
        task = self.make_task()
        self.runtime.vanish(task.provider_session_id)

        async def storm():
            return await asyncio.gather(
                self.conductor.recover_task(task.id),
                self.conductor.recover_task(task.id),
                self.conductor.recover_task(task.id))
        results = self.run_async(storm())
        self.assertEqual([r["action"] for r in results], ["resumed"] * 3)
        resumes = [c for c in self.runtime.calls if c[0] == "resume"]
        self.assertEqual(len(resumes), 1)             # one, not three

    def test_k_user_closed_surface_is_respected(self) -> None:
        task = self.make_task()
        self.surface.close(SurfaceHandle.from_dict(task.surface))
        # Progress continues; nothing reopens uninvited.
        self.runtime.emit(task.provider_session_id,
                          AgentEvent(type="progress", summary="working"))
        self.assertEqual(len(self.surface.created), 1)
        # "Show me the task" recreates exactly one surface.
        self.run_async(self.conductor.focus_task(task.id))
        self.assertEqual(len(self.surface.created), 2)

    def test_idempotent_resume(self) -> None:
        task = self.make_task()
        self.runtime.statuses[task.provider_session_id] = "running"
        self.run_async(self.conductor.resume_task(task.id))
        self.run_async(self.conductor.resume_task(task.id))
        self.assertFalse([c for c in self.runtime.calls
                          if c[0] == "resume"])       # both were no-ops

    def test_concurrent_surface_creates_yield_one_terminal(self) -> None:
        task = self.make_task()
        self.surface.close(SurfaceHandle.from_dict(task.surface))

        async def storm():
            await asyncio.gather(self.conductor.focus_task(task.id),
                                 self.conductor.focus_task(task.id),
                                 self.conductor.focus_task(task.id))
        self.run_async(storm())
        self.assertEqual(len(self.surface.created), 2)   # one recreation


if __name__ == "__main__":
    unittest.main()
