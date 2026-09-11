"""What the voice says is what the window shows.

Two surfaces carry every answer: the Boss window the user reads and the
voice they hear. They drifted apart in three ways, and the drift is worse
than either surface being wrong on its own - two slightly different
answers to one question leave the user unable to tell which is the real
one. Measured 2026-09-03, in their words: "the voice not matching the
response already throws off my trust".

1. The prompts ORDERED the paraphrase: "relay it conversationally rather
   than reading it verbatim", in both voice prompts and in the built-in
   fallback.
2. The main reply path never stripped Markdown, though every other spoken
   path did (boss_interim, boss_speaks, notifications, cards). "**Merged.**"
   reached the window with its asterisks and the voice with something to
   pronounce.
3. `note_spoken_elsewhere` had no production caller, so `covered_subjects()`
   was always empty and both of the coordinator's de-dupe nets were dead:
   news the Boss had just given was announced again a beat later, reworded.

Run with:  python3 -m unittest tests.test_spoken_matches_written -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.manager import ManagerTurn, ToolCall
from conductor.voice_coordinator import VoiceCoordinator

try:
    import conduct
except Exception:                       # the audio stack is not installed
    conduct = None


class TheVoiceIsToldToSayTheAnswerAsWritten(unittest.TestCase):
    """The prompts are the only lever on how the model renders an answer:
    the text goes onto the `speakable` channel as context, not as a string
    to read out, so what the model does with it is decided here."""

    PROMPTS = ("prompts/voice_agent.md", "prompts/voice_conductor.md")

    def test_neither_prompt_still_orders_a_paraphrase(self):
        for name in self.PROMPTS:
            with self.subTest(prompt=name):
                text = Path(name).read_text()
                self.assertNotIn("rather than reading it verbatim", text)
                self.assertNotIn("relay it conversationally", text)

    def test_both_prompts_say_the_answer_as_it_is_written(self):
        for name in self.PROMPTS:
            with self.subTest(prompt=name):
                text = Path(name).read_text()
                self.assertIn("Say the answer as it is written", text)
                self.assertIn("no summarising, no adding", text)
                self.assertIn("on the user's screen", text)

    def test_the_two_permitted_omissions_are_named(self):
        """A blanket "say every word" would collide with the rules that
        keep the voice from repeating its own interim, so the exceptions
        are spelled out rather than left to judgement."""
        for name in self.PROMPTS:
            with self.subTest(prompt=name):
                text = Path(name).read_text()
                self.assertIn("already said", text)
                self.assertIn("filler line of your own", text)

    def test_the_built_in_fallback_says_the_same(self):
        """A prompt file that fails to load must not restore the old rule
        in a build where the files are missing."""
        import boss
        self.assertNotIn("conversationally", boss.FRONTEND_INSTRUCTIONS)


@unittest.skipIf(conduct is None, "conduct needs the audio stack")
class OneAnswerReachesBothSurfaces(unittest.TestCase):
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
        self.mirrored: list[str] = []
        self.voice.mirror = type(
            "Mirror", (),
            {"mirror_prompt": lambda s, text: None,
             "mirror_answer": lambda s, text: self.mirrored.append(text)})()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def answer(self, reply: str, calls=()) -> str:
        async def turn(prompt, **kw):
            return ManagerTurn(reply=reply, tool_calls=list(calls))
        self.voice.conductor.handle_user_message = turn
        return asyncio.run(self.voice._work("what happened"))

    def test_markdown_is_gone_from_the_spoken_answer(self):
        self.assertEqual(
            self.answer("**Merged.** See `hotkey.py` for the ceiling."),
            "Merged. See hotkey.py for the ceiling.")

    def test_the_window_is_shown_the_same_string(self):
        self.answer("**Merged.** See `hotkey.py` for the ceiling.")
        self.assertEqual(self.mirrored,
                         ["Merged. See hotkey.py for the ceiling."])

    def test_a_bulleted_answer_reads_as_sentences(self):
        spoken = self.answer("Two things:\n- the cap landed\n- the poll rule "
                             "landed")
        self.assertNotIn("- ", spoken)
        self.assertIn("the cap landed", spoken)

    def test_an_answer_with_no_markdown_is_untouched(self):
        self.assertEqual(self.answer("Merged."), "Merged.")

    def test_the_late_answer_path_strips_it_too(self):
        """A turn that outlives the wait is announced later, through
        _late_answer - the same two surfaces, the same rule."""
        import inspect
        source = inspect.getsource(conduct.ConductorVoice._late_answer)
        self.assertIn("plain_text(", source)


@unittest.skipIf(conduct is None, "conduct needs the audio stack")
class TheAnswerCoversItsOwnSubjects(OneAnswerReachesBothSurfaces):
    """Before this, `covered_subjects()` was always the empty set.

    These assert on what is actually spoken, not on the queue: the point
    is the sentence the user does or does not hear a beat after the Boss
    has already told them the same thing.
    """

    def setUp(self) -> None:
        super().setUp()
        self.coordinator = VoiceCoordinator()

        def answer_covered(text, subjects):
            if text:
                self.coordinator.note_spoken_elsewhere(text)
            for subject in subjects:
                self.coordinator.note_spoken_elsewhere("", subject=subject)
        self.voice.note_spoken = answer_covered

    def announced_after(self, reply: str, calls=(), then=None) -> list[str]:
        """One Boss turn, then what the announcer says next."""
        spoken: list[str] = []

        async def speak(text: str) -> None:
            spoken.append(text)

        async def turn(prompt, **kw):
            return ManagerTurn(reply=reply, tool_calls=list(calls))

        async def scenario():
            self.coordinator.set_speaker(speak)
            self.voice.conductor.handle_user_message = turn
            await self.voice._work("what happened")
            if then is not None:
                then(self.coordinator)
            await asyncio.sleep(0.05)
        asyncio.run(scenario())
        return spoken

    def test_a_task_the_answer_was_about_is_covered(self):
        self.announced_after("Posely finished; twelve tests passed.",
                             calls=[ToolCall(tool="send_to_task",
                                             args={"task_id": "task_a"})])
        self.assertEqual(self.coordinator.covered_subjects(), {"task_a"})

    def test_the_announcer_does_not_repeat_the_news(self):
        said = self.announced_after(
            "Posely finished; twelve tests passed.",
            calls=[ToolCall(tool="task_status", args={"task_id": "task_a"})],
            then=lambda c: c.enqueue("The Posely agent finished.",
                                     "completed", subject="task_a"))
        self.assertEqual(said, [], "the same finish was said twice")

    def test_other_tasks_are_still_announced(self):
        said = self.announced_after(
            "Posely finished.",
            calls=[ToolCall(tool="task_status", args={"task_id": "task_a"})],
            then=lambda c: c.enqueue("The gptree agent needs you.",
                                     "needs_input", subject="task_b"))
        self.assertEqual(said, ["The gptree agent needs you."])

    def test_every_task_the_turn_touched_is_covered(self):
        self.announced_after(
            "Both are done.",
            calls=[ToolCall(tool="task_status", args={"task_id": "task_a"}),
                   ToolCall(tool="task_status", args={"task_id": "task_b"})])
        self.assertEqual(self.coordinator.covered_subjects(),
                         {"task_a", "task_b"})

    def test_a_turn_about_nothing_covers_nothing(self):
        self.announced_after("Hello.")
        self.assertEqual(self.coordinator.covered_subjects(), set())

    def test_the_words_are_the_backstop_when_there_is_no_task_id(self):
        """The weak half of the net, and deliberately the weak half: it
        exists for announcements that carry no subject at all."""
        said = self.announced_after(
            "Posely finished; twelve tests passed.",
            then=lambda c: c.enqueue("Posely finished, twelve tests passed.",
                                     "completed"))
        self.assertEqual(said, [])

    def test_an_unwired_voice_still_answers(self):
        """The hook is optional: a build with no coordinator must not lose
        the answer."""
        self.voice.note_spoken = None
        self.assertEqual(self.answer("Merged."), "Merged.")

    def test_a_failing_hook_does_not_lose_the_answer(self):
        def boom(text, subjects):
            raise RuntimeError("no coordinator")
        self.voice.note_spoken = boom
        self.assertEqual(self.answer("Merged."), "Merged.")


if __name__ == "__main__":
    unittest.main()
