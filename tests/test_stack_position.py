"""Ordinal reference: "the second one" must be a fact, not a derivation.

Measured, not assumed. A baseline of the reference suite made zero
wrong-task errors and four false clarifications - the Manager was not
misrouting ordinals, it was declining to resolve them, because no ordering
was defined. Defining one in the prompt made it worse: told to count
backwards from a newest-first registry it misrouted three ordinals, turning
safe refusals into confidently wrong actions (severity 4 -> 12 -> 16).

So the position is computed here and stated as data, in every place the
Manager might look. These tests pin the arithmetic the model no longer does.

Run with:  python3 -m unittest tests.test_stack_position -v
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from conductor.manager import registry_block, stack_positions, task_summary


NOW = datetime.now(timezone.utc)


def ago(seconds: int) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat().replace(
        "+00:00", "Z")


@dataclass
class FakeTask:
    id: str
    title: str = "t"
    goal: str = "g"
    status: str = "running"
    created_at: str = ""
    last_instruction_at: str | None = None


def stack(*specs) -> list:
    """specs: (id, age_seconds_ago[, status]) - oldest listed first."""
    return [FakeTask(id=s[0], title=s[0], created_at=ago(s[1]),
                     status=s[2] if len(s) > 2 else "running")
            for s in specs]


class StackPositionTest(unittest.TestCase):
    def test_oldest_is_first_the_way_the_stack_reads(self) -> None:
        tasks = stack(("a", 300), ("b", 200), ("c", 100))
        self.assertEqual(stack_positions(tasks),
                         {"a": (1, 3), "b": (2, 3), "c": (3, 3)})

    def test_registry_order_does_not_change_the_answer(self) -> None:
        """The registry prints newest first; positions must not follow it."""
        tasks = stack(("a", 300), ("b", 200), ("c", 100))
        self.assertEqual(stack_positions(list(reversed(tasks))),
                         stack_positions(tasks))

    def test_the_last_one_is_the_newest(self) -> None:
        tasks = stack(("a", 300), ("b", 200), ("c", 100))
        nth, total = stack_positions(tasks)["c"]
        self.assertEqual((nth, total), (3, 3))

    def test_recency_beats_creation_when_a_task_is_addressed(self) -> None:
        """Position follows last contact, like the tray does."""
        tasks = stack(("a", 300), ("b", 200), ("c", 100))
        tasks[0].last_instruction_at = ago(0)   # 'a' just spoken to
        self.assertEqual(stack_positions(tasks),
                         {"b": (1, 3), "c": (2, 3), "a": (3, 3)})

    def test_finished_work_has_no_position(self) -> None:
        tasks = stack(("a", 300), ("b", 200, "completed"), ("c", 100))
        self.assertEqual(stack_positions(tasks), {"a": (1, 2), "c": (2, 2)})

    def test_every_terminal_state_is_excluded(self) -> None:
        tasks = stack(("a", 400), ("b", 300, "failed"),
                      ("c", 200, "cancelled"), ("d", 100, "completed"))
        self.assertEqual(stack_positions(tasks), {"a": (1, 1)})

    def test_interrupted_still_counts(self) -> None:
        """Interrupted is unfinished and resumable, so it keeps its place."""
        tasks = stack(("a", 200, "interrupted"), ("b", 100))
        self.assertEqual(stack_positions(tasks), {"a": (1, 2), "b": (2, 2)})

    def test_a_single_session_is_first_and_last(self) -> None:
        self.assertEqual(stack_positions(stack(("a", 100))), {"a": (1, 1)})

    def test_no_sessions(self) -> None:
        self.assertEqual(stack_positions([]), {})


class RenderingTest(unittest.TestCase):
    def test_the_registry_states_the_position(self) -> None:
        block = registry_block(stack(("a", 300), ("b", 200), ("c", 100)))
        self.assertIn("On screen: 1st of 3", block)
        self.assertIn("On screen: 2nd of 3", block)
        self.assertIn("On screen: 3rd of 3 (the last one)", block)

    def test_only_the_newest_is_marked_the_last_one(self) -> None:
        block = registry_block(stack(("a", 200), ("b", 100)))
        self.assertEqual(block.count("(the last one)"), 1)

    def test_finished_rows_carry_no_position(self) -> None:
        block = registry_block(stack(("a", 200), ("b", 100, "completed")))
        self.assertIn("On screen: 1st of 1", block)
        self.assertEqual(block.count("On screen"), 1)

    def test_ordinals_past_the_named_list_still_render(self) -> None:
        block = registry_block(stack(*[(f"t{i}", 500 - i) for i in range(12)]))
        self.assertIn("On screen: 11th of 12", block)
        self.assertIn("On screen: 12th of 12 (the last one)", block)

    def test_task_summary_carries_it_when_given(self) -> None:
        task = stack(("a", 100))[0]
        self.assertNotIn("on_screen", task_summary(task))
        self.assertEqual(task_summary(task, (2, 5))["on_screen"], "2 of 5")


if __name__ == "__main__":
    unittest.main()
