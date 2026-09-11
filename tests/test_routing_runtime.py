"""Where each worker runs, chosen per task.

Choosing at launch meant editing a constant in boss.py and restarting, and
it applied to every worker after it. But the choice is genuinely per task -
"run this one in the cloud so I can check it from my phone" and "this one
needs my uncommitted changes" are both ordinary, in the same sitting.

The invariant that matters: everything after creation must reach the same
runtime that started the session. Sending a follow-up to a cloud worker
through the local runtime would not fail loudly, it would look for a tmux
pane that does not exist.

Run with:  python3 -m unittest tests.test_routing_runtime -v
"""

from __future__ import annotations

import unittest
from unittest import mock

from conductor.routing_runtime import RoutingRuntime


def fake(name: str):
    # spec'd, or the mock invents every attribute and "does this runtime
    # offer peek?" is always yes - which is the opposite of what the
    # dispatch has to decide.
    from conductor.runtime import CodingAgentRuntime
    rt = mock.AsyncMock(spec=CodingAgentRuntime)
    rt.name = name
    rt.create_session = mock.AsyncMock(return_value=f"sess_{name}")
    rt.get_status = mock.AsyncMock(return_value="running")
    return rt


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.local, self.cloud = fake("local"), fake("cloud")
        self.rt = RoutingRuntime(local=self.local, cloud=self.cloud)


class ChoosingPerTask(Base):
    async def test_the_default_is_local(self):
        await self.rt.create_session("t1", "/w", "go")
        self.local.create_session.assert_awaited_once()
        self.cloud.create_session.assert_not_awaited()

    async def test_a_task_can_ask_for_the_cloud(self):
        self.rt.want("t1", "cloud")
        await self.rt.create_session("t1", "/w", "go")
        self.cloud.create_session.assert_awaited_once()
        self.local.create_session.assert_not_awaited()

    async def test_the_request_applies_to_one_task_only(self):
        """Otherwise asking once quietly moves everything after it, which
        is the launch-time flag all over again."""
        self.rt.want("t1", "cloud")
        await self.rt.create_session("t1", "/w", "go")
        await self.rt.create_session("t2", "/w", "go")
        self.assertEqual(self.cloud.create_session.await_count, 1)
        self.assertEqual(self.local.create_session.await_count, 1)

    async def test_an_unknown_location_falls_to_the_default(self):
        self.rt.want("t1", "mars")
        await self.rt.create_session("t1", "/w", "go")
        self.local.create_session.assert_awaited_once()

    async def test_the_default_can_be_the_cloud(self):
        rt = RoutingRuntime(local=self.local, cloud=self.cloud,
                            default="cloud")
        await rt.create_session("t1", "/w", "go")
        self.cloud.create_session.assert_awaited_once()

    async def test_with_no_cloud_runtime_everything_is_local(self):
        rt = RoutingRuntime(local=self.local, cloud=None, default="cloud")
        rt.want("t1", "cloud")
        await rt.create_session("t1", "/w", "go")
        self.local.create_session.assert_awaited_once()
        self.assertEqual(rt.available(), ("local",))


class StayingWithTheSameRuntime(Base):
    """A session belongs to whichever runtime made it, for its whole life."""

    async def setup_cloud_worker(self) -> str:
        self.rt.want("t1", "cloud")
        return await self.rt.create_session("t1", "/w", "go")

    async def test_follow_ups_reach_the_runtime_that_started_it(self):
        sid = await self.setup_cloud_worker()
        await self.rt.send(sid, "also fix the tests")
        self.cloud.send.assert_awaited_once_with(sid, "also fix the tests")
        self.local.send.assert_not_awaited()

    async def test_status_interrupt_and_destroy_follow_too(self):
        sid = await self.setup_cloud_worker()
        await self.rt.get_status(sid)
        await self.rt.interrupt(sid)
        await self.rt.destroy(sid)
        self.cloud.get_status.assert_awaited()
        self.cloud.interrupt.assert_awaited()
        self.cloud.destroy.assert_awaited()
        self.local.interrupt.assert_not_awaited()

    async def test_two_workers_in_different_places_do_not_cross(self):
        self.rt.want("t1", "cloud")
        cloud_sid = await self.rt.create_session("t1", "/w", "go")
        local_sid = await self.rt.create_session("t2", "/w", "go")
        await self.rt.send(cloud_sid, "to the cloud one")
        await self.rt.send(local_sid, "to the local one")
        self.cloud.send.assert_awaited_once_with(cloud_sid, "to the cloud one")
        self.local.send.assert_awaited_once_with(local_sid, "to the local one")

    async def test_a_session_from_before_a_restart_falls_to_the_default(self):
        """The map is memory; the sessions outlive it."""
        await self.rt.get_status("sess_from_yesterday")
        self.local.get_status.assert_awaited()

    async def test_the_location_is_reportable(self):
        sid = await self.setup_cloud_worker()
        self.assertEqual(self.rt.location_of(sid), "cloud")
        self.assertEqual(self.rt.location_of("unknown"), "local")


class CapabilitiesOnlyOneSideHas(Base):
    async def test_a_cloud_only_call_reaches_the_cloud_worker(self):
        self.rt.want("t1", "cloud")
        sid = await self.rt.create_session("t1", "/w", "go")
        self.cloud.peek = mock.AsyncMock(return_value={"ok": True})
        await self.rt.peek(sid)
        self.cloud.peek.assert_awaited_once_with(sid)

    async def test_a_capability_nobody_has_is_an_attribute_error(self):
        with self.assertRaises(AttributeError):
            self.rt.no_such_capability


class TheTranscriptIsTheLocalOnes(Base):
    """CodingAgentRuntime declares `transcript = None` as a class
    attribute, so the router inherited one. Ordinary lookup succeeded,
    __getattr__ never ran, and _surface_request's
    `getattr(runtime, "transcript", None)` was always None - every
    SurfaceRequest went out with transcript_path unset. Nothing failed,
    because the registered surfaces attach to a PTY and ignore it.
    """

    def test_it_is_the_local_runtimes(self):
        self.local.transcript = "the-local-execution-transcript"
        self.assertEqual(self.rt.transcript,
                         "the-local-execution-transcript")

    def test_the_inherited_default_does_not_shadow_it(self):
        """The exact failure: the ABC's None won, silently."""
        from conductor.runtime import CodingAgentRuntime
        self.assertIsNone(CodingAgentRuntime.transcript)
        self.local.transcript = "real"
        self.assertIsNotNone(self.rt.transcript)

    def test_the_cloud_half_is_never_asked(self):
        """A cloud session writes no transcript at all, teleported or
        not, so there is only ever one to report."""
        self.local.transcript = "local"
        self.cloud.transcript = "cloud"
        self.assertEqual(self.rt.transcript, "local")

    def test_a_runtime_with_no_transcript_dir_still_reports_none(self):
        self.local.transcript = None
        self.assertIsNone(self.rt.transcript)

    def test_the_surface_request_carries_it(self):
        """The invariant that was actually broken, at the call site that
        broke it."""
        import inspect

        from conductor.global_conductor import GlobalConductor
        src = inspect.getsource(GlobalConductor._surface_request)
        self.assertIn('getattr(self.runtime, "transcript", None)', src)
        self.local.transcript = "real"
        self.assertEqual(getattr(self.rt, "transcript", None), "real")


class PlainAttributesTheHalvesExpose(Base):
    """Not everything one runtime offers is a method. Forwarding
    callables only made such an attribute read as missing rather than as
    the value it has."""

    def test_a_data_attribute_reaches_the_runtime_that_has_it(self):
        self.cloud.dashboard_url = "https://claude.ai/code"
        self.assertEqual(self.rt.dashboard_url, "https://claude.ai/code")

    def test_registration_order_settles_a_tie(self):
        """No call, so no session id to route by - the same convention
        the unrouted call path uses."""
        self.local.marker = "local"
        self.cloud.marker = "cloud"
        self.assertEqual(self.rt.marker, "local")

    def test_a_present_none_is_an_answer_not_a_miss(self):
        """Falling through on None would hand out the other runtime's."""
        self.local.marker = None
        self.cloud.marker = "cloud"
        self.assertIsNone(self.rt.marker)

    def test_methods_still_route_by_session(self):
        """The data path must not shadow the dispatch it sits in front
        of."""
        self.cloud.peek = mock.AsyncMock(return_value={"ok": True})
        self.assertTrue(callable(self.rt.peek))

    def test_an_attribute_nobody_has_still_raises(self):
        with self.assertRaises(AttributeError):
            self.rt.no_such_attribute


if __name__ == "__main__":
    unittest.main()
