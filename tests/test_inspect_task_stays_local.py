"""Asking what a local worker is doing does not teleport it from the cloud.

inspect_task looks inside a cloud worker by peeking - `claude --teleport`
into a throwaway checkout, with a 60 s timeout. It chose to peek by asking
the runtime's capabilities: is this session readable? For a session the
routing map had never heard of - every worker after a restart, because
that map lives in memory - the question went to the first runtime that had
is_readable, the cloud one. It had never heard of the worker either, said
"not readable", and peek spent its whole minute on a worker sitting in a
local tmux pane.

Measured from the logs, 2026-09-10 05:47Z to 2026-09-11 00:40Z: 51
inspect_task calls on workers from before a restart took a median 61.0 s
(0.04 s on workers started since), and 16 utterances typed into the Boss
while one ran waited a median 45 s to be read. focus_task already believes
the router's placement; inspect_task now does too.

Run with:  python3 -m unittest tests.test_inspect_task_stays_local -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from conductor.global_conductor import GlobalConductor
from conductor.routing_runtime import RoutingRuntime
from tests.test_routing_runtime import fake


def conductor_on(runtime):
    gc = GlobalConductor.__new__(GlobalConductor)
    gc.runtime = runtime
    gc._emit = lambda *a, **k: None
    gc._touch = lambda *a, **k: None
    gc.subagent_for = mock.AsyncMock(
        return_value=mock.Mock(status="running", result=None))
    task = mock.Mock(id="task_a", project_id="p",
                     provider_session_id="sess-1")
    project = mock.Mock()
    project.inspect_task = lambda task_id: {"task_id": task_id}
    gc._find_task = lambda task_id, project_id=None: (project, task)
    return gc


def inspect(gc) -> dict:
    return asyncio.run(gc.inspect_task("task_a"))


class AfterARestart(unittest.TestCase):
    """The real router, the way a restart leaves it: an empty owner map."""

    def router(self):
        local, cloud = fake("local"), fake("cloud")
        local.reconcile_session = mock.AsyncMock(return_value="healthy")
        local.pending_approvals = mock.AsyncMock(return_value=[])
        cloud.is_readable = mock.Mock(return_value=False)
        cloud.peek = mock.AsyncMock(return_value={"ok": True, "said": "hi"})
        return RoutingRuntime(local=local, cloud=cloud), cloud

    def test_a_local_worker_from_before_the_restart_is_not_peeked(self):
        router, cloud = self.router()
        report = inspect(conductor_on(router))
        cloud.peek.assert_not_awaited()
        self.assertNotIn("where", report)
        self.assertEqual(report["provider_health"], "healthy")

    def test_a_worker_the_router_placed_in_the_cloud_still_is(self):
        router, cloud = self.router()
        router.owner["sess-1"] = "cloud"
        cloud.reconcile_session = mock.AsyncMock(return_value="healthy")
        cloud.pending_approvals = mock.AsyncMock(return_value=[])
        report = inspect(conductor_on(router))
        cloud.peek.assert_awaited_once_with("sess-1")
        self.assertEqual((report["where"], report["latest"]), ("cloud", "hi"))


class ByPlacement(unittest.TestCase):
    def runtime(self, location, readable=False):
        runtime = mock.Mock()
        runtime.location_of = lambda session_id: location
        runtime.is_readable = lambda session_id: readable
        runtime.peek = mock.AsyncMock(return_value={"ok": True, "said": "x"})
        runtime.reconcile_session = mock.AsyncMock(return_value="healthy")
        runtime.pending_approvals = mock.AsyncMock(return_value=[])
        return runtime

    def test_local_is_not_peeked(self):
        runtime = self.runtime("local")
        inspect(conductor_on(runtime))
        runtime.peek.assert_not_awaited()

    def test_cloud_is_peeked(self):
        runtime = self.runtime("cloud")
        self.assertEqual(inspect(conductor_on(runtime))["where"], "cloud")
        runtime.peek.assert_awaited_once()

    def test_a_teleported_cloud_worker_is_already_readable(self):
        runtime = self.runtime("cloud", readable=True)
        inspect(conductor_on(runtime))
        runtime.peek.assert_not_awaited()

    def test_a_runtime_that_does_not_route_keeps_the_capability_rule(self):
        """No location_of: no opinion on where the session lives, so the
        rule from before routing stands - peek what is not readable."""
        runtime = mock.Mock(spec=["peek", "is_readable", "reconcile_session",
                                  "pending_approvals"])
        runtime.is_readable = lambda session_id: False
        runtime.peek = mock.AsyncMock(return_value={"ok": False})
        runtime.reconcile_session = mock.AsyncMock(return_value="healthy")
        runtime.pending_approvals = mock.AsyncMock(return_value=[])
        inspect(conductor_on(runtime))
        runtime.peek.assert_awaited_once()

    def test_a_location_lookup_that_fails_is_no_opinion(self):
        runtime = self.runtime("cloud")

        def broken(session_id):
            raise RuntimeError("router gone")
        runtime.location_of = broken
        inspect(conductor_on(runtime))
        runtime.peek.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
