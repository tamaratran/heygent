"""A notification the user was meant to hear actually gets heard.

Two ways one went missing tonight, both from the log:

    07:30:44  task.completed  "I'll pull PR #29 from GitHub and read..."
    07:30:44  task.completed  "PR #29 exists: ..."          <- the answer
    07:31:13  voice.speech_interrupted
              "The Explain PR 29 agent finished. I'll pull PR #29..."

The answer never reached the user: both completions were the same
instruction, so the second deduplicated into the first, and what got
queued was the plan. Then the user interrupted THAT - in the very second
it started - to ask why notifications were not being read out, and the
interruption deleted it.

The frontend then explained: "Yep, that's intentional. I keep the noisy
agent notifications out of your way." It was not intentional.

Two invariants stand, and these tests keep them: twenty turn-ends on one
instruction are one notification and one popup, and a sentence the user
deliberately cut off is not replayed at them.

Run with:  python3 -m unittest tests.test_notifications_reach_the_user -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from unittest import mock

from conductor.notifications import (NotificationPolicy, NotificationService,
                                     NotificationStore)
from conductor.observability import ObservabilityEvent
from conductor.voice_coordinator import VoiceCoordinator


class CutOffBeforeItCouldBeHeard(unittest.TestCase):
    def test_an_immediately_interrupted_line_is_said_after_the_user(self):
        spoken = []
        release = asyncio.Event()
        first = {"call": True}

        async def speak(text):
            if first["call"]:
                first["call"] = False
                await release.wait()       # blocked in its first moment...
            spoken.append(text)

        async def drive():
            c = VoiceCoordinator(speak=speak)
            c.enqueue("The Explain PR 29 agent finished.", "completed",
                      subject="task_a")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            c.interrupt_for_user()         # ...the user presses to talk
            await asyncio.sleep(0)
            c.user_finished()
            for _ in range(30):
                await asyncio.sleep(0.01)
                if spoken:
                    break
        asyncio.run(drive())
        self.assertEqual(spoken, ["The Explain PR 29 agent finished."],
                         "the interruption deleted the notification")

    def test_cut_off_after_hearing_it_is_not_replayed(self):
        """The rule that stands: replaying what the user deliberately cut
        off would nag. Past the first moment, they heard enough."""
        started, said = [], []

        async def speak(text):
            started.append(text)
            await asyncio.sleep(1.0)
            said.append(text)

        async def drive():
            c = VoiceCoordinator(speak=speak)
            c.REPLAY_IF_CUT_WITHIN_S = 0.05
            c.enqueue("A long sentence about a finished task.", "completed",
                      subject="task_a")
            await asyncio.sleep(0.15)      # well past the first moment
            c.interrupt_for_user()
            await asyncio.sleep(0)
            c.user_finished()
            await asyncio.sleep(0.05)
        asyncio.run(drive())
        self.assertEqual(len(started), 1, "restarted a sentence they cut off")
        self.assertEqual(said, [])

    def test_cut_off_twice_means_it(self):
        attempts = {"n": 0}

        async def speak(text):
            attempts["n"] += 1
            await asyncio.sleep(1.0)

        async def drive():
            c = VoiceCoordinator(speak=speak)
            c.enqueue("An agent finished.", "completed", subject="task_a")
            for _ in range(3):
                await asyncio.sleep(0.01)
                c.interrupt_for_user()
                await asyncio.sleep(0)
                c.user_finished()
            await asyncio.sleep(0.05)
        asyncio.run(drive())
        self.assertEqual(attempts["n"], 2, "requeued without limit")

    def test_the_rule_can_be_switched_off(self):
        """0 restores "never replay", so the old behaviour is one constant
        away if this turns out to nag."""
        self.assertGreater(VoiceCoordinator.REPLAY_IF_CUT_WITHIN_S, 0)
        self.assertLess(VoiceCoordinator.REPLAY_IF_CUT_WITHIN_S, 3)


def completed(task_id, summary, event_id):
    return ObservabilityEvent(type="task.completed", component="task",
                              task_id=task_id, event_id=event_id,
                              data={"summary": summary, "success": True})


class TheLatestAnswerIsWhatGetsSaid(unittest.TestCase):
    def service(self):
        self.tmp = tempfile.TemporaryDirectory()
        svc = NotificationService.__new__(NotificationService)
        svc.store = NotificationStore(self.tmp.name)
        svc.policy = NotificationPolicy()
        svc._awaiting = set()
        svc.conductor = mock.Mock()
        svc._names = lambda tid: ("proj", "voice-agent", "Explain PR 29")
        self.popups, self.superseded = [], []
        svc.on_activity = None
        svc.on_notify = self.popups.append
        svc.on_supersede = self.superseded.append
        svc.on_resolve = None
        return svc

    def tearDown(self):
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_a_later_turn_on_the_same_instruction_replaces_the_text(self):
        svc = self.service()
        svc.handle_event(completed("task_a", "I'll pull PR #29 and read it", "e1"))
        svc.handle_event(completed("task_a", "PR #29 exists: it does X", "e2"))
        stored = [n for n in svc.store.list(task_id="task_a")
                  if n.type == "completed"]
        self.assertEqual(len(stored), 1, "two notifications for one instruction")
        self.assertEqual(stored[0].body, "PR #29 exists: it does X",
                         "the user was left with the plan, not the result")
        self.assertEqual(len(self.popups), 1, "a second popup")
        self.assertEqual([n.body for n in self.superseded],
                         ["PR #29 exists: it does X"])

    def test_twenty_turns_are_still_one_notification_and_one_popup(self):
        svc = self.service()
        for i in range(20):
            svc.handle_event(completed("task_a", f"turn {i} done", f"e{i}"))
        stored = [n for n in svc.store.list(task_id="task_a")
                  if n.type == "completed"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(len(self.popups), 1)

    def test_once_read_later_turns_are_chatter(self):
        """Latest-wins is for a notification the user has not seen. After
        they have, a worker talking to itself must stay quiet."""
        svc = self.service()
        svc.handle_event(completed("task_a", "done: the answer", "e1"))
        svc.store.mark_task_read("task_a")
        svc.handle_event(completed("task_a", "still poking around", "e2"))
        self.assertEqual(self.superseded, [])
        stored = [n for n in svc.store.list(task_id="task_a")
                  if n.type == "completed"]
        self.assertEqual(stored[0].body, "done: the answer")

    def test_a_replay_of_the_same_text_changes_nothing(self):
        svc = self.service()
        svc.handle_event(completed("task_a", "the answer", "e1"))
        svc.handle_event(completed("task_a", "the answer", "e1"))
        self.assertEqual(self.superseded, [])
        self.assertEqual(len(self.popups), 1)


class SupersedingReplacesSpeechAndNeverAddsIt(unittest.TestCase):
    def test_pending_speech_about_the_task_is_replaced(self):
        said = []

        async def speak(text):
            said.append(text)

        async def drive():
            c = VoiceCoordinator(speak=speak)
            c.interrupt_for_user()                 # hold the queue
            c.enqueue("The agent finished. I'll pull PR #29.", "completed",
                      subject="task_a")
            self.assertTrue(c.has_pending("task_a"))
            c.enqueue("The agent finished. PR #29 exists: X.", "completed",
                      subject="task_a")
            c.user_finished()
            await asyncio.sleep(0.05)
        asyncio.run(drive())
        self.assertEqual(said, ["The agent finished. PR #29 exists: X."])

    def test_nothing_pending_means_nothing_is_added(self):
        """has_pending is the gate the product uses: with the earlier line
        already spoken, a superseding turn must not speak again."""
        c = VoiceCoordinator(speak=lambda t: None)
        self.assertFalse(c.has_pending("task_a"))


class ItIsWiredIntoTheProduct(unittest.TestCase):
    def test_conduct_supersedes_only_pending_speech(self):
        from pathlib import Path
        source = Path("conduct.py").read_text()
        body = source[source.index("def on_supersede"):]
        body = body[:body.index("def on_resolve")]
        self.assertIn("has_pending", body)
        self.assertIn("on_supersede=on_supersede", source)


class WorkersAreToldTheMachineIsInUse(unittest.TestCase):
    def test_every_brief_carries_the_rule(self):
        """Asserted on the brief a worker actually receives, not on the
        source: a first version of this test read the constant's own
        definition and passed with the rule never sent."""
        from conductor.conductor import SHARED_MACHINE_RULE, build_worker_prompt
        from conductor.task_types import Task
        self.assertIn("on this machine", SHARED_MACHINE_RULE)
        self.assertIn("do not play audio", SHARED_MACHINE_RULE)
        task = Task(id="task_a", project_id="p", title="Verify barge-in",
                    goal="Observe a real session", status="starting")
        brief = build_worker_prompt(task, "")
        self.assertIn(SHARED_MACHINE_RULE, brief,
                      "the rule exists but no worker is told it")

    def test_what_is_opened_for_the_user_comes_to_the_front(self):
        """Measured: a worker opened Chrome tabs for the user with `open -g`,
        they landed behind the voice session, and the user had to ask for
        them. The Boss and every worker are told the default is the front,
        and the exception is the user asking for the background."""
        from pathlib import Path
        from conductor.claude_manager import MANAGER_PROMPT_VERSION
        from conductor.conductor import SHARED_MACHINE_RULE
        self.assertIn("bring it to the front", SHARED_MACHINE_RULE)
        self.assertIn("no -g", SHARED_MACHINE_RULE)
        self.assertIn("asked for it in the background", SHARED_MACHINE_RULE)
        manager = Path("prompts/manager.md").read_text()
        self.assertIn(f"# Manager ({MANAGER_PROMPT_VERSION})", manager)
        self.assertIn("## Opening things for the user", manager)
        self.assertIn("brings it to the front", manager)
        self.assertIn("without -g", manager)
        worker = Path("prompts/claude_worker.md").read_text()
        self.assertIn("bring it to the front", worker)

    def test_a_change_is_never_shown_by_opening_a_window(self):
        """Asked 2026-09-11: workers opened a new Chrome window after every
        change, and one landed in the user's screen recording. Pages the
        user has open reload themselves, so the Boss and every worker are
        told to open nothing the user did not ask to see."""
        from pathlib import Path
        from conductor.conductor import SHARED_MACHINE_RULE
        self.assertIn("Open nothing they did not ask to see", SHARED_MACHINE_RULE)
        self.assertIn("reload themselves", SHARED_MACHINE_RULE)
        self.assertIn("without opening anything visible", SHARED_MACHINE_RULE)
        manager = Path("prompts/manager.md").read_text()
        section = manager.partition("## Opening things for the user")[2] \
            .partition("\n## ")[0]
        self.assertIn("never shown by opening", section)
        self.assertIn("reload themselves", section)
        worker = Path("prompts/claude_worker.md").read_text()
        self.assertIn("Open nothing they did not ask", worker)


class TheFrontendDoesNotInventProductBehaviour(unittest.TestCase):
    def test_the_prompt_says_not_to_answer_questions_about_itself(self):
        from pathlib import Path
        text = Path("prompts/voice_agent.md").read_text()
        self.assertIn("this product's own behaviour", text)
        self.assertIn("never answer",
                      text[:text.index("this product's own")].lower())


class TheUserHearsOneAssistant(unittest.TestCase):
    """Asked 2026-09-11: the Boss talked about workers and agents and said
    "I don't have web access". The Boss, its orientation and the voice in
    front of it are all told the user hears one assistant that does the
    work, and never a limitation."""

    def test_the_boss_is_told_how_it_sounds(self):
        from pathlib import Path
        from conductor.claude_manager import load_manager_prompt
        prompt = load_manager_prompt()
        self.assertIn("## How you sound", prompt)
        self.assertIn("To the user you are one assistant", prompt)
        self.assertIn("Never announce what you cannot do", prompt)
        self.assertIn("I don't have web access", prompt)
        self.assertNotIn("say so\nplainly", prompt)
        self.assertNotIn("start coding agents in them", prompt)
        self.assertIn("# Manager (manager-v26)",
                      Path("prompts/manager.md").read_text())

    def test_having_no_web_tools_is_not_something_to_say(self):
        from conductor.boss_tools import ORIENTATION
        self.assertIn("never a reason to tell the user you cannot",
                      ORIENTATION)

    def test_the_voice_speaks_as_one_assistant(self):
        from pathlib import Path
        text = Path("prompts/voice_conductor.md").read_text()
        body = text.partition("\n---\n")[2]
        self.assertIn("To the user you are one assistant", body)
        self.assertIn("I don't have web access", body)


class TheAssistantKnowsWhoItIs(unittest.TestCase):
    """Asked 2026-09-11: it should know its name is heygent and that it is
    one persistent voice agent rather than a new agent per task. The Boss,
    which writes the answers, and the voice, which speaks them, both say
    so."""

    NAME = "Your name is heygent."
    WHAT = ("Rather than opening a new agent for every task, you are\n"
            "one persistent voice agent that can coordinate work across the "
            "user's\ncomputer.")

    def test_the_boss_knows_its_name_and_what_it_is(self):
        from conductor.claude_manager import load_manager_prompt
        prompt = load_manager_prompt()
        self.assertIn("## Who you are", prompt)
        self.assertIn(self.NAME, prompt)
        self.assertIn(self.WHAT, prompt)
        self.assertIn("introduce the job once, as heygent", prompt)

    def test_the_voice_knows_its_name_and_what_it_is(self):
        from pathlib import Path
        body = Path("prompts/voice_conductor.md").read_text() \
            .partition("\n---\n")[2]
        self.assertIn(self.NAME, body)
        self.assertIn(self.WHAT, body)


if __name__ == "__main__":
    unittest.main()
