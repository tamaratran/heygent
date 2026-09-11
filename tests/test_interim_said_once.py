"""An answer the interim already spoke is not spoken again.

A slow Boss turn speaks its first sentence while the tools run (the
interim), and the final answer is spoken with that sentence cut off the
front. When the interim WAS the whole answer, the cut left nothing - and
the code fell back to the whole answer, so the user heard the same
paragraph twice, back to back. Measured 2026-08-30 23:34:44Z: interim and
final 4 ms apart, identical text.

Run with:  python3 -m unittest tests.test_interim_said_once -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.manager import ManagerTurn

try:
    import conduct
except Exception:                       # the audio stack is not installed
    conduct = None


@unittest.skipIf(conduct is None, "conduct needs the audio stack")
class TheInterimIsSaidOnce(unittest.TestCase):
    def setUp(self) -> None:
        import voice_agent
        from conductor.global_conductor import GlobalConductor
        from conductor.testing import FakeCodingAgentRuntime, \
            FakeWorkspaceManager
        from tests.test_global import FakeManagerBackend
        self.tmp = tempfile.TemporaryDirectory()
        real_transcript = voice_agent.TRANSCRIPT_LOG
        voice_agent.TRANSCRIPT_LOG = Path(self.tmp.name) / "session.jsonl"
        self.addCleanup(setattr, voice_agent, "TRANSCRIPT_LOG",
                        real_transcript)
        real_speaker = voice_agent.Speaker
        voice_agent.Speaker = lambda: type("S", (), {"speaking": False,
                                                     "flush": lambda s: None})()
        self.addCleanup(setattr, voice_agent, "Speaker", real_speaker)
        conductor = GlobalConductor(
            home=Path(self.tmp.name) / "home",
            runtime=FakeCodingAgentRuntime(), manager=FakeManagerBackend(),
            workspace_factory=lambda project: FakeWorkspaceManager())
        self.voice = conduct.ConductorVoice("", None, conductor)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def answer(self, reply: str, interim: str) -> str:
        """One turn whose first sentence was spoken as the interim
        while it ran, then answered with `reply`."""
        voice = self.voice

        async def turn(prompt, **kw):
            voice.boss_interim = interim      # what on_interim did meanwhile
            return ManagerTurn(reply=reply)
        voice.conductor.handle_user_message = turn
        return asyncio.run(voice._work("what is PR 109"))

    def test_an_interim_that_was_the_whole_answer_is_not_repeated(self):
        said = "The repeat was the voice layer, not me. PR 109 fixes it."
        self.assertEqual(self.answer(said, said), "",
                         "the whole answer was spoken a second time")

    def test_what_follows_the_interim_is_still_said(self):
        first = "Looking at PR 109 now."
        self.assertEqual(self.answer(first + " It frees the worker slots.",
                                     first),
                         "It frees the worker slots.")

    def test_an_interim_with_markup_still_matches(self):
        said = "PR 109 is about dead windows."
        self.assertEqual(self.answer("**PR 109** is about dead windows.",
                                     said), "")

    def test_an_unrelated_interim_leaves_the_answer_alone(self):
        self.assertEqual(self.answer("Merged.", "Checking the branch first."),
                         "Merged.")

    def test_no_interim_means_the_answer_as_is(self):
        self.assertEqual(self.answer("Merged.", ""), "Merged.")


if __name__ == "__main__":
    unittest.main()
