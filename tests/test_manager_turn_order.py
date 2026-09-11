"""A second thing said while the Manager is busy gets its own turn and its
own answer - and no work item stays open for ever.

The Manager is one Claude session with one response stream. Measured on a
live run: two utterances 25 s apart produced two concurrent Manager turns
on that stream, the CLI folded the second message into the first turn (one
result for both), the newer caller took that result with an empty reply,
and the older caller waited for a result that never came. Its work item
stayed open on the voice side, which mutes every later reply - so the user
heard nothing more about either request until the session was restarted.

Run with:  python3 -m unittest tests.test_manager_turn_order -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.conductor import Conductor
from conductor.manager import ManagerBackend, ManagerTurn
from conductor.observability import ObservabilityBus, ObservabilityEvent
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager

try:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    from conductor.claude_manager import ClaudeManagerBackend
except Exception:          # pragma: no cover - exercised by skipping
    ClaudeManagerBackend = None


class FakeCli:
    """The client as the backend drives it, with the CLI's measured queue
    behaviour behind it: a message sent mid-turn is folded into the running
    turn and gets no result of its own."""

    def __init__(self) -> None:
        self.stream: asyncio.Queue = asyncio.Queue()
        self.in_turn = False
        self.queries: list[str] = []
        self.folded: list[str] = []

    async def query(self, message: str) -> None:
        text = message.rsplit("User says: ", 1)[1]
        self.queries.append(text)
        if self.in_turn:
            self.folded.append(text)
            return
        self.in_turn = True
        asyncio.get_running_loop().create_task(self._turn(text))

    async def _turn(self, text: str) -> None:
        await asyncio.sleep(0.01)
        await self.stream.put(AssistantMessage(
            content=[TextBlock(text=f"reply to {text}")], model="fake"))
        await asyncio.sleep(0.01)
        self.in_turn = False
        await self.stream.put(ResultMessage(
            subtype="success", duration_ms=0, duration_api_ms=0,
            is_error=False, num_turns=1, session_id="sess_fake"))

    async def receive_response(self):
        while True:
            message = await self.stream.get()
            yield message
            if isinstance(message, ResultMessage):
                return


@unittest.skipIf(ClaudeManagerBackend is None, "needs claude-agent-sdk")
class SerializedTurnsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = ObservabilityBus()
        self.events: list[ObservabilityEvent] = []
        self.bus.subscribe(self.events.append)
        self.conductor = Conductor(self.tmp.name, FakeCodingAgentRuntime(),
                                   workspaces=FakeWorkspaceManager(),
                                   bus=self.bus)
        self.backend = ClaudeManagerBackend()
        self.cli = None

        async def connect(conductor):
            if self.cli is None:
                self.cli = FakeCli()
            self.backend._conductor = conductor
            return self.cli

        self.backend._connect = connect

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_two_utterances_get_two_turns_and_their_own_answers(self):
        async def both():
            return await asyncio.wait_for(asyncio.gather(
                self.backend.handle("first thing", self.conductor),
                self.backend.handle("second thing", self.conductor)), 5)

        first, second = asyncio.run(both())
        self.assertEqual(first.reply, "reply to first thing")
        self.assertEqual(second.reply, "reply to second thing")
        # In the order they were said, and never mid-turn.
        self.assertEqual(self.cli.queries, ["first thing", "second thing"])
        self.assertEqual(self.cli.folded, [])

    def test_the_wait_is_visible(self):
        async def both():
            return await asyncio.wait_for(asyncio.gather(
                self.backend.handle("first thing", self.conductor),
                self.backend.handle("second thing", self.conductor)), 5)

        asyncio.run(both())
        queued = [e for e in self.events if e.type == "manager.turn_queued"]
        self.assertEqual([e.data["text"] for e in queued], ["second thing"])

    def test_busy_only_while_a_turn_is_in_flight(self):
        seen: list[bool] = []

        async def run():
            seen.append(self.backend.busy)
            task = asyncio.ensure_future(
                self.backend.handle("first thing", self.conductor))
            await asyncio.sleep(0)
            seen.append(self.backend.busy)
            await asyncio.wait_for(task, 5)
            seen.append(self.backend.busy)

        asyncio.run(run())
        self.assertEqual(seen, [False, True, False])


try:
    import conduct
    from conductor.global_conductor import GlobalConductor
except Exception:          # pragma: no cover - exercised by skipping
    conduct = None


class _SlowBackend(ManagerBackend):
    """Finishes when told to, however long that takes."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def handle(self, text: str, conductor) -> ManagerTurn:
        await self.release.wait()
        return ManagerTurn(reply=f"finished: {text}")


@unittest.skipIf(conduct is None, "conduct needs the audio stack")
class WorkAlwaysClosesTest(unittest.TestCase):
    def setUp(self) -> None:
        import boss
        import voice_agent
        self.tmp = tempfile.TemporaryDirectory()
        real_transcript = voice_agent.TRANSCRIPT_LOG
        voice_agent.TRANSCRIPT_LOG = Path(self.tmp.name) / "session.jsonl"
        self.addCleanup(setattr, voice_agent, "TRANSCRIPT_LOG",
                        real_transcript)
        real_timeout = boss.MANAGER_TURN_TIMEOUT_S
        boss.MANAGER_TURN_TIMEOUT_S = 0.05
        self.addCleanup(setattr, boss, "MANAGER_TURN_TIMEOUT_S", real_timeout)
        real_speaker = voice_agent.Speaker
        voice_agent.Speaker = lambda: type("S", (), {"speaking": False,
                                                     "flush": lambda s: None})()
        self.addCleanup(setattr, voice_agent, "Speaker", real_speaker)

        self.backend = _SlowBackend()
        self.conductor = GlobalConductor(
            home=Path(self.tmp.name) / "home",
            runtime=FakeCodingAgentRuntime(), manager=self.backend,
            workspace_factory=lambda project: FakeWorkspaceManager())
        self.voice = conduct.ConductorVoice("", None, self.conductor)
        self.announced: list[str] = []

        async def announce(text):
            self.announced.append(text)

        self.voice.announce = announce

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_slow_turn_closes_its_work_and_answers_later(self):
        async def run():
            self.voice.in_flight.add("item_1")
            await self.voice._run_claude("item_1", "count the PRs")
            # The work item is closed while the turn is still running...
            self.assertNotIn("item_1", self.voice.in_flight)
            self.assertEqual(len(self.announced), 1)
            self.assertIn("still in progress", self.announced[0])
            # ...and its answer is spoken when it does arrive.
            self.backend.release.set()
            for _ in range(20):
                await asyncio.sleep(0.01)
                if len(self.announced) > 1:
                    break
            self.assertEqual(len(self.announced), 2)
            self.assertIn("finished: count the PRs", self.announced[1])
            self.assertIn("count the PRs", self.announced[1])

        asyncio.run(run())

    def test_a_prompt_turn_is_unchanged(self):
        async def run():
            self.backend.release.set()
            self.voice.in_flight.add("item_1")
            await self.voice._run_claude("item_1", "count the PRs")
            self.assertEqual(self.announced, ["finished: count the PRs"])
            self.assertNotIn("item_1", self.voice.in_flight)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
