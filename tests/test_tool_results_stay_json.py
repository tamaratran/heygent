"""A tool result the Boss receives is always JSON it can parse.

Measured over the live bridge: list_tasks came back exactly 4000
characters long, sliced mid-escape ("\\u"), and json.loads refused it.
_serialize ended with `json.dumps(result)[:4000]` - a cut through the
text, not the data. The invisible Boss read results as prose and could
shrug at a ragged end; the visible Boss reads them over MCP as data, and
a long goal in one task made every list_tasks unparseable.

Run with:  python3 -m unittest tests.test_tool_results_stay_json -v
"""

from __future__ import annotations

import json
import unittest

from conductor.claude_manager import _serialize, RESULT_LIMIT
from conductor.task_types import Task


def task(i: int, goal_len: int) -> Task:
    return Task(id=f"task_{i:08x}", title=f"Task {i}", goal="g" * goal_len,
                project_id="proj_x", status="waiting_for_user")


class ResultsStayJson(unittest.TestCase):
    def test_a_short_result_is_untouched(self):
        out = _serialize([task(1, 40), task(2, 40)])
        rows = json.loads(out)
        self.assertEqual([r["title"] for r in rows], ["Task 1", "Task 2"])
        self.assertEqual(rows[0]["goal"], "g" * 40)

    def test_a_long_result_is_shortened_not_sliced(self):
        """Long fields lose their tails; the JSON keeps its shape."""
        out = _serialize([task(i, 3000) for i in range(6)])
        self.assertLessEqual(len(out), RESULT_LIMIT)
        rows = json.loads(out)                          # the whole point
        self.assertEqual(len(rows), 6)
        for row in rows:
            self.assertTrue(row["goal"].startswith("ggg"))
            self.assertLess(len(row["goal"]), 3000)
            self.assertTrue(row["goal"].endswith("…"))

    def test_a_result_too_long_even_when_shortened_keeps_what_fits(self):
        out = _serialize([task(i, 500) for i in range(400)])
        self.assertLessEqual(len(out), RESULT_LIMIT)
        payload = json.loads(out)
        self.assertIsInstance(payload, dict)
        self.assertGreater(len(payload["items"]), 0)
        self.assertEqual(payload["omitted"], 400 - len(payload["items"]))

    def test_a_dict_with_a_long_context_still_parses(self):
        out = _serialize({"task": {"id": "task_1"}, "context": "c" * 20000,
                          "recent_events": [{"type": "x", "text": "y" * 5000}]})
        self.assertLessEqual(len(out), RESULT_LIMIT)
        payload = json.loads(out)
        self.assertEqual(payload["task"]["id"], "task_1")

    def test_none_is_still_ok(self):
        self.assertEqual(_serialize(None), "ok")


if __name__ == "__main__":
    unittest.main()
