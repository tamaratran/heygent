"""Atomic persistence tests: a failed state mutation must never leave
half-valid canonical state.

Run with:  python3 -m unittest tests.test_persistence -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from conductor import TaskStore
from conductor.storage import atomic_write_json, read_json
from conductor.task_store import CorruptStateError


class AtomicWriteTest(unittest.TestCase):
    def test_normal_write_and_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_write_json(path, {"a": 1})
            atomic_write_json(path, {"a": 2})
            self.assertEqual(read_json(path), {"a": 2})

    def test_leftover_tmp_file_is_harmless(self) -> None:
        """Simulates a crash after writing the temp file but before rename:
        the canonical file must still be the previous valid state."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_write_json(path, {"good": True})
            # The crash artifact: a half-written temp file next to it.
            (Path(tmp) / "state.json.tmp").write_text('{"partial": ')
            self.assertEqual(read_json(path), {"good": True})
            # A new writer just replaces the leftover on its next write.
            atomic_write_json(path, {"good": 2})
            self.assertEqual(read_json(path), {"good": 2})


class StoreRecoveryTest(unittest.TestCase):
    def test_corrupt_state_refuses_to_load(self) -> None:
        """Silently starting fresh would overwrite the last recoverable
        state on the next mutation - so a corrupt file must be loud."""
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(tmp)
            store.create("t", "g")
            state_path = Path(tmp) / ".myconductor/state.json"
            state_path.write_text('{"version": 1, "tasks": {  broken')
            with self.assertRaises(CorruptStateError):
                TaskStore(tmp)
            # And the corrupt file was not touched by the failed load.
            self.assertIn("broken", state_path.read_text())

    def test_missing_file_starts_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(tmp)
            self.assertEqual(store.list(), [])

    def test_state_file_is_always_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(tmp)
            for i in range(20):
                store.create(f"t{i}", "g")
            data = json.loads(
                (Path(tmp) / ".myconductor/state.json").read_text())
            self.assertEqual(len(data["tasks"]), 20)


if __name__ == "__main__":
    unittest.main()
