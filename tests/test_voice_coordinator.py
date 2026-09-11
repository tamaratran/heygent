"""VoiceCoordinator tests: one voice, one stream, priority, barge-in - the
spec's section 27 acceptance behavior without audio.

Run with:  python3 -m unittest tests.test_voice_coordinator -v
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from conductor.voice_coordinator import VoiceCoordinator


class RecordingBackend:
    """A fake speech backend that records concurrency and order."""

    def __init__(self, seconds: float = 0.02) -> None:
        self.seconds = seconds
        self.order: list[str] = []
        self.active = 0
        self.max_active = 0
        self.cancelled: list[str] = []

    async def speak(self, text: str) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.seconds)
            self.order.append(text)
        except asyncio.CancelledError:
            self.cancelled.append(text)
            raise
        finally:
            self.active -= 1


class SerializationTest(unittest.TestCase):
    def test_two_simultaneous_notifications_never_overlap(self) -> None:
        backend = RecordingBackend()

        async def scenario():
            coordinator = VoiceCoordinator(speak=backend.speak)
            coordinator.enqueue("Worker A finished.", "completed")
            coordinator.enqueue("Worker B finished.", "completed")
            await asyncio.sleep(0.1)
        asyncio.run(scenario())
        self.assertEqual(backend.max_active, 1)          # <= 1 stream, ever
        self.assertEqual(backend.order,
                         ["Worker A finished.", "Worker B finished."])

    def test_priority_speaks_approvals_before_completions(self) -> None:
        backend = RecordingBackend()

        async def scenario():
            coordinator = VoiceCoordinator(speak=backend.speak)
            coordinator.enqueue("B completed.", "completed")
            coordinator.enqueue("A needs your approval.", "needs_input")
            await asyncio.sleep(0.1)
        asyncio.run(scenario())
        # Both queued before speech started: the approval outranks.
        self.assertEqual(backend.order[0], "A needs your approval.")

    def test_empty_text_is_ignored(self) -> None:
        backend = RecordingBackend()

        async def scenario():
            coordinator = VoiceCoordinator(speak=backend.speak)
            coordinator.enqueue("   ", "completed")
            await asyncio.sleep(0.05)
            return coordinator.pending()
        self.assertEqual(asyncio.run(scenario()), 0)
        self.assertEqual(backend.order, [])


class BargeInTest(unittest.TestCase):
    def test_user_speech_interrupts_and_queue_waits(self) -> None:
        backend = RecordingBackend(seconds=0.2)

        async def scenario():
            coordinator = VoiceCoordinator(speak=backend.speak)
            coordinator.enqueue("A long announcement.", "completed")
            coordinator.enqueue("A second announcement.", "completed")
            await asyncio.sleep(0.05)            # A is mid-speech
            coordinator.interrupt_for_user()     # user starts talking
            await asyncio.sleep(0.1)
            held = backend.order.copy()          # nothing spoken meanwhile
            coordinator.user_finished()
            await asyncio.sleep(0.3)
            return held
        held = asyncio.run(scenario())
        self.assertIn("A long announcement.", backend.cancelled)  # yielded
        self.assertEqual(held, [])               # quiet while user spoke
        # A was cut off in its first moment - the user pressed to talk as
        # it began, not in reaction to it - so it is said once more, first,
        # once they finish. Then B. Still one stream at a time.
        self.assertEqual(backend.order, ["A long announcement.",
                                         "A second announcement."])
        self.assertEqual(backend.max_active, 1)

    def test_system_never_interrupts_system(self) -> None:
        """A newly arriving notification never cancels one mid-speech -
        only the user can do that."""
        backend = RecordingBackend(seconds=0.1)

        async def scenario():
            coordinator = VoiceCoordinator(speak=backend.speak)
            coordinator.enqueue("First.", "completed")
            await asyncio.sleep(0.03)            # mid-speech
            coordinator.enqueue("Urgent approval.", "needs_input")
            await asyncio.sleep(0.3)
        asyncio.run(scenario())
        self.assertEqual(backend.cancelled, [])  # nothing was cut off
        self.assertEqual(backend.order, ["First.", "Urgent approval."])


class SingleVoiceTest(unittest.TestCase):
    def test_one_configured_voice_for_everything(self) -> None:
        coordinator = VoiceCoordinator(voice="TestVoice")
        self.assertEqual(coordinator.voice, "TestVoice")
        # The backend is the coordinator's own; components enqueue text and
        # kind only - there is no per-notification voice parameter at all.
        self.assertNotIn("voice", VoiceCoordinator.enqueue.__code__.co_varnames)


if __name__ == "__main__":
    unittest.main()


class OneStreamTest(unittest.TestCase):
    """activeUserFacingSpeechStreams <= 1, by construction.

    The overlap was never a queueing bug: notifications ran through macOS
    `say` while the manager spoke through the live session, so two engines
    produced two streams in two voices. The coordinator owns no engine now,
    and its single path is the live session itself.
    """

    def test_no_second_engine_exists_anywhere(self) -> None:
        root = Path(__file__).resolve().parent.parent
        files = [root / "conduct.py", root / "voice_agent.py"]
        files += sorted((root / "conductor").glob("*.py"))
        for path in files:
            text = path.read_text()
            for banned in ('"say"', "'say'", "afplay", "NSSpeechSynthesizer"):
                self.assertNotIn(banned, text,
                                 f"{path.name} can start a second voice "
                                 f"({banned})")

    def test_coordinator_without_a_path_stays_silent(self) -> None:
        """No fallback engine: unwired means silent, never a second voice."""
        c = VoiceCoordinator(voice="marin")
        self.assertIsNone(c._speak)

    def test_never_two_concurrent_utterances(self) -> None:
        async def drive():
            live = {"n": 0, "max": 0}

            async def speak(text):
                live["n"] += 1
                live["max"] = max(live["max"], live["n"])
                await asyncio.sleep(0.05)
                live["n"] -= 1

            c = VoiceCoordinator(speak=speak, voice="marin")
            for i in range(6):
                c.enqueue(f"message {i}", kind="completion")
            for _ in range(200):
                if not c.is_speaking() and not c.pending():
                    break
                await asyncio.sleep(0.02)
            return live["max"]

        self.assertEqual(asyncio.run(drive()), 1,
                         "two utterances played at once")

    def test_speech_path_is_the_live_session(self) -> None:
        """conduct.py must hand the coordinator the session's own announce."""
        root = Path(__file__).resolve().parent.parent
        text = (root / "conduct.py").read_text()
        self.assertIn("voice.set_speaker(agent.announce)", text)


class SemanticQueueTest(unittest.TestCase):
    """Sections 19-21: the queue carries meaning, not just order."""

    def drain(self, build):
        async def go():
            said = []
            c = VoiceCoordinator(speak=lambda t: said.append(t), voice="marin")
            build(c)
            for _ in range(200):
                if not c.is_speaking() and not c.pending():
                    break
                await asyncio.sleep(0.01)
            return said
        return asyncio.run(go())

    def test_stale_progress_is_dropped_for_the_result(self) -> None:
        said = self.drain(lambda c: (
            c.enqueue("I am still checking the repo.", subject="repo-check"),
            c.enqueue("The repo check is complete.", subject="repo-check")))
        self.assertEqual(said, ["The repo check is complete."])

    def test_resolved_subject_is_never_spoken(self) -> None:
        def build(c):
            c.enqueue("Billing needs your approval.", kind="needs_input",
                      subject="appr-1")
            c.enqueue("Posely finished.", kind="completed", subject="posely")
            c.drop_subject("appr-1")          # the user approved it by voice
        said = self.drain(build)
        self.assertEqual(said, ["Posely finished."])

    def test_three_completions_coalesce_into_one_sentence(self) -> None:
        def build(c):
            for name in ("Posely login", "billing", "the test app"):
                c.enqueue(f"{name} finished.", kind="completed", subject=name)
            c.coalesce()
        said = self.drain(build)
        self.assertEqual(len(said), 1, said)
        self.assertIn("3 tasks just finished", said[0])
        self.assertIn("billing", said[0])

    def test_coverage_after_the_fact_drops_a_queued_repeat(self) -> None:
        """The answer may land while the announcement is already queued."""
        def build(c):
            c.enqueue("The Posely task completed.", kind="completed",
                      subject="posely")
            c.note_spoken_elsewhere("Yes, it finished.", subject="posely")
        self.assertEqual(self.drain(build), [])

    def test_what_the_conversation_covered_is_not_repeated(self) -> None:
        def build(c):
            c.note_spoken_elsewhere(
                "Yes, Posely just finished and all 12 tests passed.",
                subject="posely")
            c.enqueue("The Posely task completed, all 12 tests passed.",
                      kind="completed", subject="posely")
        self.assertEqual(self.drain(build), [])

    def test_unrelated_news_still_gets_through(self) -> None:
        def build(c):
            c.note_spoken_elsewhere("Yes, Posely finished, 12 tests passed.",
                                    subject="posely")
            c.enqueue("The billing migration failed.", kind="failed",
                      subject="billing")
        self.assertEqual(self.drain(build), ["The billing migration failed."])

    def test_barge_in_does_not_replay_the_interrupted_sentence(self) -> None:
        async def go():
            said, started = [], []

            async def speak(text):
                started.append(text)
                await asyncio.sleep(0.3)
                said.append(text)

            c = VoiceCoordinator(speak=speak, voice="marin")
            # Past the first moment: the user heard what this was and cut
            # it off on purpose. (A cut within REPLAY_IF_CUT_WITHIN_S is the
            # other case - they were not yet reacting to it - and is said
            # once more; see test_notifications_reach_the_user.)
            c.REPLAY_IF_CUT_WITHIN_S = 0.02
            c.enqueue("The Posely task completed and all of the tests",
                      subject="posely")
            await asyncio.sleep(0.05)
            c.interrupt_for_user()            # the user talks over it
            await asyncio.sleep(0.05)
            c.user_finished()
            for _ in range(60):
                if not c.is_speaking() and not c.pending():
                    break
                await asyncio.sleep(0.01)
            return started, said

        started, said = asyncio.run(go())
        self.assertEqual(len(started), 1, "the sentence was restarted")
        self.assertEqual(said, [], "an interrupted sentence still completed")


class VoiceArchitectureTest(unittest.TestCase):
    """Spec section 25: the invariants that must hold by construction, not
    by discipline. A second speech path is how two voices started talking
    over each other, so the assertion is that no such path exists."""

    def _sources(self):
        root = Path(__file__).resolve().parent.parent
        files = [root / "conduct.py"] + sorted((root / "conductor").glob("*.py"))
        return [(p, p.read_text()) for p in files
                if p.name != "voice_coordinator.py"]

    def test_no_component_creates_its_own_playback(self) -> None:
        for path, text in self._sources():
            # macOS `say` belongs here: it was the second engine, and it is
            # what let a system voice talk over the live session's voice.
            for banned in ("sd.play(", "sd.OutputStream(", "afplay",
                           "AVSpeechSynthesizer", "NSSpeechSynthesizer",
                           '"say"', "'say'"):
                self.assertNotIn(
                    banned, text,
                    f"{path.name} opens its own audio path ({banned}); all "
                    "speech must go through the VoiceCoordinator")

    def test_exactly_one_coordinator_is_constructed(self) -> None:
        root = Path(__file__).resolve().parent.parent
        made = sum(p.read_text().count("VoiceCoordinator(")
                   for p in [root / "conduct.py"]
                   + sorted((root / "conductor").glob("*.py"))
                   if p.name != "voice_coordinator.py")
        self.assertEqual(made, 1, "there must be exactly one voice for the "
                                  f"whole UI, found {made} constructions")

    def test_one_voice_configuration_for_every_notification(self) -> None:
        """No kind of notification may pick its own voice."""
        async def drive():
            spoken = []

            async def say(text):
                spoken.append((text, c.voice))

            c = VoiceCoordinator(speak=say, voice="marin")
            for kind in ("approval", "failure", "completion", "info"):
                c.enqueue(f"{kind} message", kind=kind)
            for _ in range(40):
                if not c.is_speaking() and not c.pending():
                    break
                await asyncio.sleep(0.02)
            return spoken

        spoken = asyncio.run(drive())
        self.assertEqual(len(spoken), 4)
        self.assertEqual({voice for _t, voice in spoken}, {"marin"})
