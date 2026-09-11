"""A task that names its CLI is started by that CLI's runtime.

Phase 2 of docs/any-cli.md: `create_task(provider="gemini")` records the
provider on the task, asks the router for that provider's runtime, and
the worker is hosted there with the Gemini adapter - the same cmux
workspace, a different CLI inside it.

Run with:  python3 -m unittest tests.test_provider_routing -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.conductor import Conductor
from conductor.routing_runtime import RoutingRuntime
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager


class Named(FakeCodingAgentRuntime):
    def __init__(self, label):
        super().__init__()
        self.label = label
        self.started: list[str] = []

    async def create_session(self, task_id, working_directory, initial_prompt):
        self.started.append(task_id)
        sid = await super().create_session(task_id, working_directory,
                                           initial_prompt)
        # Distinct ids per runtime, as real providers give: two fakes
        # counting from one would hand the router the same id twice.
        return f"{self.label}-{sid}"


class TheRouterKnowsProviders(unittest.TestCase):
    def test_a_provider_is_a_destination_like_a_place(self):
        local, gemini = Named("local"), Named("gemini")
        router = RoutingRuntime(local=local, providers={"gemini": gemini})
        self.assertIn("gemini", router.runtimes)

        async def go():
            router.want("task_g", "gemini")
            sid = await router.create_session("task_g", "/tmp", "go")
            sid2 = await router.create_session("task_c", "/tmp", "go")
            return sid, sid2
        sid, sid2 = asyncio.run(go())
        self.assertEqual(gemini.started, ["task_g"])
        self.assertEqual(local.started, ["task_c"])
        self.assertEqual(router.location_of(sid), "gemini")
        self.assertEqual(router.location_of(sid2), "local")

    def test_an_unknown_provider_falls_back_to_the_default(self):
        local = Named("local")
        router = RoutingRuntime(local=local)
        router.want("task_x", "cursor")
        asyncio.run(router.create_session("task_x", "/tmp", "go"))
        self.assertEqual(local.started, ["task_x"])


class TheCapabilityProbeSeesEveryProviderTheRouterDrives(unittest.TestCase):
    """Measured in the Boss's own CLAUDE.md: "Codex: not available
    (runtime does not drive it)" while a Codex runtime sat in the
    router - so the Boss, told in writing that it could not, did not."""

    def test_the_router_names_what_it_drives(self):
        local, gemini, codex = Named("local"), Named("gemini"), Named("codex")
        gemini.provider, codex.provider = "gemini", "codex"
        router = RoutingRuntime(local=local, providers={"gemini": gemini,
                                                        "codex": codex})
        self.assertEqual(router.providers, ["claude-code", "gemini", "codex"])
        self.assertEqual(RoutingRuntime(local=local).providers, ["claude-code"])

    def test_the_snapshot_reports_them_available(self):
        from unittest import mock
        from conductor.capabilities import snapshot
        local, codex = Named("local"), Named("codex")
        codex.provider = "codex"
        conductor = mock.Mock()
        conductor.runtime = RoutingRuntime(local=local, providers={"codex": codex})
        conductor.locator = None
        conductor.surfaces = {}
        with mock.patch("conductor.capabilities.shutil.which",
                        side_effect=lambda b: f"/bin/{b}"):
            state = snapshot(conductor)
        self.assertEqual(state["Codex"], (True, ""))
        self.assertEqual(state["Claude Code"], (True, ""))
        self.assertEqual(state["Cursor"], (False, "runtime does not drive it"))


class TheConductorRecordsAndRoutesTheProvider(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / ".git").mkdir()
        self.local, self.gemini = Named("local"), Named("gemini")
        self.router = RoutingRuntime(local=self.local,
                                     providers={"gemini": self.gemini})
        self.conductor = Conductor(root, runtime=self.router,
                                   workspaces=FakeWorkspaceManager())

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_task_with_a_provider(self):
        task = asyncio.run(self.conductor.create_task("Fix login", "goal",
                                                      provider="gemini"))
        self.assertEqual(task.provider, "gemini")
        self.assertEqual(self.gemini.started, [task.id])
        self.assertEqual(self.local.started, [])
        self.assertEqual(self.conductor.store.get(task.id).provider, "gemini")

    def test_the_default_is_claude_code_and_location_still_routes_it(self):
        task = asyncio.run(self.conductor.create_task("Fix login", "goal"))
        self.assertEqual(task.provider, "claude-code")
        self.assertEqual(self.local.started, [task.id])

    def test_an_unknown_provider_is_refused_before_anything_starts(self):
        with self.assertRaises(ValueError):
            asyncio.run(self.conductor.create_task("x", "goal", provider="nonesuch"))
        self.assertEqual(self.local.started, [])
        self.assertEqual(self.gemini.started, [])


if __name__ == "__main__":
    unittest.main()
