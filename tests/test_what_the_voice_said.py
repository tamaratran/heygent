"""The Boss can find out what the voice said aloud - by asking, not by
having it typed into its window.

The voice answers greetings and small talk itself and fills the gap
while the Boss works, so the user hears things the Boss never said, and
the Boss's own reply to the same words can then repeat or contradict
them. Typing "Voice said: ..." into the Boss's window would have put
words between the user's own lines in the transcript they read. So it
is a tool: what_the_voice_said lists the last things said, newest last,
with their age; the timeline keeps them as VOICE lines; the window shows
nothing.

Run with:  python3 -m unittest tests.test_what_the_voice_said -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor.boss_session import BossSessionEvent, render_timeline
from conductor.global_conductor import GlobalConductor, MANAGER_TOOLS, _age
from conductor.manager import ManagerTurn
from conductor.observability import ObservabilityBus
from conductor.testing import FakeCodingAgentRuntime


class RecordingBoss:
    """A Manager that keeps a timeline, like the visible Boss does."""

    def __init__(self):
        self.recorded = []

    def record_voice_said(self, text):
        self.recorded.append(text)

    async def handle(self, text, conductor):
        return ManagerTurn(reply="ok")


class PlainBoss:
    """A Manager with no timeline (the invisible SDK Boss)."""

    async def handle(self, text, conductor):
        return ManagerTurn(reply="ok")


class TheBossAsks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def conductor(self, manager=None):
        return GlobalConductor(home=Path(self.tmp.name) / "home",
                               runtime=FakeCodingAgentRuntime(),
                               manager=manager or PlainBoss(),
                               bus=ObservabilityBus(), search_roots=[])

    def test_it_is_a_manager_tool(self):
        self.assertIn("what_the_voice_said", MANAGER_TOOLS)

    def test_nothing_said_yet(self):
        gc = self.conductor()
        self.assertEqual(gc.what_the_voice_said(), "The voice has said nothing yet.")

    def test_what_was_said_newest_last_with_its_age(self):
        gc = self.conductor()
        gc.voice_spoke("Hey! How's it going?")
        gc.voice_spoke("Let me check on that.")
        self.assertEqual(gc.what_the_voice_said(),
                         "0s ago: Hey! How's it going?\n"
                         "0s ago: Let me check on that.")

    def test_the_age_is_told_plainly(self):
        self.assertEqual(_age(3), "3s ago")
        self.assertEqual(_age(125), "2m ago")
        self.assertEqual(_age(7300), "2h ago")
        self.assertEqual(_age(-1), "0s ago")

    def test_only_the_last_dozen_are_kept(self):
        gc = self.conductor()
        for n in range(20):
            gc.voice_spoke(f"line {n}")
        lines = gc.what_the_voice_said().splitlines()
        self.assertEqual(len(lines), GlobalConductor.VOICE_SAID_KEEP)
        self.assertTrue(lines[0].endswith("line 8"))
        self.assertTrue(lines[-1].endswith("line 19"))

    def test_empty_and_whitespace_are_nothing(self):
        gc = self.conductor()
        gc.voice_spoke("")
        gc.voice_spoke("   \n ")
        self.assertEqual(gc.what_the_voice_said(), "The voice has said nothing yet.")

    def test_whitespace_is_folded(self):
        gc = self.conductor()
        gc.voice_spoke("Sure,\n  one   moment.")
        self.assertTrue(gc.what_the_voice_said().endswith("Sure, one moment."))

    def test_it_reaches_the_boss_timeline_when_there_is_one(self):
        boss = RecordingBoss()
        gc = self.conductor(boss)
        gc.voice_spoke("Hi there!")
        self.assertEqual(boss.recorded, ["Hi there!"])

    def test_a_boss_without_a_timeline_is_fine(self):
        gc = self.conductor(PlainBoss())
        gc.voice_spoke("Hi there!")     # no record_voice_said: nothing to do
        self.assertIn("Hi there!", gc.what_the_voice_said())

    def test_it_is_dispatched_like_any_tool(self):
        gc = self.conductor()
        gc.voice_spoke("Hello!")
        result = asyncio.run(gc.handle_action("what_the_voice_said", {}))
        self.assertEqual(result, "0s ago: Hello!")


class TheTimelineReadsAsTheConversation(unittest.TestCase):
    def test_voice_lines_render_between_the_others(self):
        events = [
            BossSessionEvent("e1", "boss_x", 1, "user_message",
                             payload={"text": "hey", "source": "voice"}),
            BossSessionEvent("e2", "boss_x", 2, "voice_message",
                             payload={"text": "Hey! How's it going?"}),
            BossSessionEvent("e3", "boss_x", 3, "boss_message",
                             payload={"text": "All quiet, two workers running."}),
        ]
        text = render_timeline(events)
        self.assertIn("YOU\n  hey\n", text)
        self.assertIn("VOICE\n  Hey! How's it going?\n", text)
        self.assertLess(text.index("VOICE"), text.index("BOSS"))


class TheVisibleBossRecordsIt(unittest.TestCase):
    def test_record_voice_said_is_a_voice_message(self):
        from conductor.pty_manager import PtyManagerBackend
        backend = PtyManagerBackend.__new__(PtyManagerBackend)
        backend.session = mock.Mock(id="boss_x")
        backend.record = mock.Mock()
        PtyManagerBackend.record_voice_said(backend, "Hi there!")
        backend.record.assert_called_once_with("voice_message", {"text": "Hi there!"})

    def test_no_session_no_record(self):
        from conductor.pty_manager import PtyManagerBackend
        backend = PtyManagerBackend.__new__(PtyManagerBackend)
        backend.session = None
        backend.record = mock.Mock()
        PtyManagerBackend.record_voice_said(backend, "Hi there!")
        backend.record.assert_not_called()


try:
    import voice_agent
except Exception:          # pragma: no cover - needs the audio stack
    voice_agent = None


@unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
class TheVoiceTellsTheConductor(unittest.TestCase):
    def test_the_base_hook_is_quiet(self):
        agent = voice_agent.VoiceAgent(api_key="", ui=None, allow_write=False,
                                       bus=ObservabilityBus())
        self.assertIsNone(agent._reply_spoken("hello"))

    def test_the_conductor_voice_hands_it_over(self):
        try:
            import conduct
        except Exception:
            self.skipTest("conduct needs the audio stack")
        fake = mock.Mock()
        fake.bus = ObservabilityBus()
        agent = conduct.ConductorVoice(api_key="", ui=None, conductor=fake)
        agent._reply_spoken("Hey! How's it going?")
        fake.voice_spoke.assert_called_once_with("Hey! How's it going?")

    def test_a_failing_record_does_not_break_the_voice(self):
        try:
            import conduct
        except Exception:
            self.skipTest("conduct needs the audio stack")
        fake = mock.Mock()
        fake.bus = ObservabilityBus()
        fake.voice_spoke.side_effect = RuntimeError("disk gone")
        agent = conduct.ConductorVoice(api_key="", ui=None, conductor=fake)
        agent._reply_spoken("Hey!")      # logged, not raised


if __name__ == "__main__":
    unittest.main()
