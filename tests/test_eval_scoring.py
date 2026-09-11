"""The scorer decides what counts as a right answer, so loosening it needs
its own tests.

"also_accept" was added because several read-only routes can be equally
correct: "Did the billing one finish?" is answered by inspecting that task
or by searching sessions for it, and once a task is retired from the open
roster searching is the better route. Scoring one of them as the only
correct answer measures the harness's taste rather than the Manager's
reference resolution.

What must NOT loosen is the part that matters: acting on the wrong task,
or acting at all when the case expects a question.

Run with:  python3 -m unittest tests.test_eval_scoring -v
"""

from __future__ import annotations

import unittest

from conductor.evals import EvalCase, score


def case(expected: dict, name: str = "c") -> EvalCase:
    return EvalCase(name=name, user_message="m", expected=expected)


def call(tool: str, **args) -> dict:
    return {"tool": tool, "args": args}


class AlsoAcceptTest(unittest.TestCase):
    def test_the_primary_route_still_passes(self) -> None:
        c = case({"action": "inspect_task", "task_id": "task_bill",
                  "also_accept": ["search_sessions"]})
        self.assertTrue(score(c, [call("inspect_task",
                                       task_id="task_bill")]).passed)

    def test_an_accepted_alternative_passes(self) -> None:
        c = case({"action": "inspect_task", "task_id": "task_bill",
                  "also_accept": ["search_sessions"]})
        self.assertTrue(score(c, [call("search_sessions",
                                       query="billing")]).passed)

    def test_an_unlisted_route_still_fails(self) -> None:
        c = case({"action": "inspect_task", "task_id": "task_bill",
                  "also_accept": ["search_sessions"]})
        result = score(c, [call("list_projects")])
        self.assertFalse(result.passed)

    def test_the_primary_route_is_still_held_to_the_task_id(self) -> None:
        """The alternative route is free, but naming the wrong task on the
        primary one is the failure the suite exists to catch."""
        c = case({"action": "inspect_task", "task_id": "task_bill",
                  "also_accept": ["search_sessions"]})
        result = score(c, [call("inspect_task", task_id="task_auth")])
        self.assertFalse(result.passed)

    def test_also_accept_never_licenses_a_mutation(self) -> None:
        c = case({"action": "inspect_task", "task_id": "task_bill",
                  "also_accept": ["search_sessions"]})
        result = score(c, [call("cancel_task", task_id="task_bill")])
        self.assertFalse(result.passed)
        self.assertEqual(result.kind, "wrong_action")

    def test_absent_also_accept_behaves_exactly_as_before(self) -> None:
        c = case({"action": "inspect_task", "task_id": "task_bill"})
        self.assertTrue(score(c, [call("inspect_task",
                                       task_id="task_bill")]).passed)
        self.assertFalse(score(c, [call("search_sessions",
                                        query="billing")]).passed)


class SeverityStillBitesTest(unittest.TestCase):
    """The numbers this suite is steered by. Acting on the wrong agent must
    stay far more expensive than asking - the prompt change that raised the
    pass rate while tripling severity was caught by exactly this weighting."""

    def test_wrong_task_outweighs_a_false_clarification(self) -> None:
        c = case({"action": "interrupt_task", "task_id": "task_a"})
        wrong = score(c, [call("interrupt_task", task_id="task_b")])
        asked = score(c, [])
        self.assertEqual(wrong.kind, "wrong_task")
        self.assertEqual(asked.kind, "false_clarification")
        self.assertGreater(wrong.severity, asked.severity)

    def test_cancelling_the_wrong_task_is_the_worst_case(self) -> None:
        c = case({"action": "cancel_task", "task_id": "task_a"})
        result = score(c, [call("cancel_task", task_id="task_b")])
        self.assertEqual(result.severity, 9)

    def test_acting_when_a_question_was_wanted_fails(self) -> None:
        c = case({"action": "clarify"})
        result = score(c, [call("interrupt_task", task_id="task_a")])
        self.assertFalse(result.passed)
        self.assertEqual(result.kind, "acted_instead_of_clarifying")

    def test_asking_when_a_question_was_wanted_passes(self) -> None:
        self.assertTrue(score(case({"action": "clarify"}), []).passed)


if __name__ == "__main__":
    unittest.main()
