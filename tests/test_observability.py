"""Observability tests: events, traces, redaction, sinks, and the
handle_action seam.

Run with:  python3 -m unittest tests.test_observability -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from conductor.conductor import Conductor
from conductor.observability import (JsonlSink, ObservabilityBus,
                                     ObservabilityEvent, new_trace)
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


class BusTest(unittest.TestCase):
    def test_emit_subscribe_unsubscribe(self) -> None:
        bus = ObservabilityBus()
        seen = []
        unsubscribe = bus.subscribe(seen.append)
        bus.emit(ObservabilityEvent(type="task.created", component="task"))
        unsubscribe()
        bus.emit(ObservabilityEvent(type="task.created", component="task"))
        self.assertEqual(len(seen), 1)

    def test_broken_sink_does_not_break_emit(self) -> None:
        bus = ObservabilityBus()
        seen = []
        bus.subscribe(lambda e: 1 / 0)
        bus.subscribe(seen.append)
        bus.emit(ObservabilityEvent(type="x", component="conductor"))
        self.assertEqual(len(seen), 1)

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            ObservabilityEvent(type="x", component="mainframe")
        with self.assertRaises(ValueError):
            ObservabilityEvent(type="x", component="task", severity="loud")


class VerbatimTest(unittest.TestCase):
    def test_event_data_is_written_verbatim(self) -> None:
        """These traces are local and for the owner: payloads are never
        scrubbed, so what the viewer shows is what actually happened."""
        bus = ObservabilityBus()
        seen = []
        bus.subscribe(seen.append)
        bus.emit(ObservabilityEvent(
            type="manager.tool_call", component="manager",
            data={"args": {"message": "use api_key=abc123 for this"}}))
        self.assertEqual(seen[0].data["args"]["message"],
                         "use api_key=abc123 for this")


class JsonlSinkTest(unittest.TestCase):
    def test_writes_daily_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = ObservabilityBus()
            bus.subscribe(JsonlSink(tmp))
            bus.emit(ObservabilityEvent(type="task.created", component="task",
                                        task_id="task_x"))
            files = list((Path(tmp) / ".myconductor/observability/events")
                         .glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            entry = json.loads(files[0].read_text().splitlines()[0])
            self.assertEqual(entry["type"], "task.created")
            self.assertEqual(entry["task_id"], "task_x")
            self.assertIn("event_id", entry)


class InstrumentedConductorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = ObservabilityBus()
        self.events: list[ObservabilityEvent] = []
        self.bus.subscribe(self.events.append)
        self.runtime = FakeCodingAgentRuntime()
        self.conductor = Conductor(self.tmp.name, self.runtime,
                                   workspaces=FakeWorkspaceManager(),
                                   bus=self.bus)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def test_create_task_emits_lifecycle_events(self) -> None:
        asyncio.run(self.conductor.create_task("t", "g"))
        for expected in ("task.created", "workspace.creating",
                         "workspace.created", "runtime.session_created"):
            self.assertIn(expected, self.types())

    def test_one_interaction_shares_one_trace(self) -> None:
        trace_id = new_trace()
        asyncio.run(self.conductor.handle_action(
            "create_task", {"title": "t", "goal": "g"}))
        traced = [e for e in self.events if e.trace_id == trace_id]
        self.assertEqual(len(traced), len(self.events))
        self.assertGreaterEqual(len(traced), 4)

    def test_handle_action_flow(self) -> None:
        task = asyncio.run(self.conductor.handle_action(
            "create_task", {"title": "Fix login", "goal": "g"}))
        self.events.clear()
        asyncio.run(self.conductor.handle_action(
            "send_to_task", {"task_id": task.id,
                             "message": "Don't touch OAuth"}))
        self.assertEqual(self.types()[0], "conductor.action_received")
        self.assertIn("conductor.task_resolved", self.types())
        self.assertIn("task.message_sent", self.types())
        executed = [e for e in self.events
                    if e.type == "conductor.action_executed"]
        self.assertEqual(len(executed), 1)
        self.assertIsNotNone(executed[0].duration_ms)

    def test_handle_action_rejects_unknown_tool(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(self.conductor.handle_action("run_bash",
                                                     {"cmd": "rm -rf /"}))
        self.assertIn("conductor.error", self.types())

    def test_handle_action_unknown_task(self) -> None:
        with self.assertRaises(KeyError):
            asyncio.run(self.conductor.handle_action(
                "interrupt_task", {"task_id": "task_missing"}))
        self.assertIn("conductor.error", self.types())


if __name__ == "__main__":
    unittest.main()
