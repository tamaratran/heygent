"""Worker turns reach the Boss as they happen.

Every meaningful turn of a worker the Boss created is typed into the
Boss's session by the Conductor - the deterministic pipeline decides
what is meaningful (SupervisorInbox), the Boss is told, and it does not
have to be asked. Other Bosses' workers are not its business, and an
update never lands in the middle of a spoken answer.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from conductor.agent_events import AgentEvent
from conductor.pty_manager import PtyManagerBackend, whole_sentences
from conductor.supervisor_inbox import SupervisoryEvent
from tests.test_boss_session import FakeBridge, FakeConductor, FakeRuntime, fake_helper


def supervisory(task_id: str, type_: str = "completed", summary: str = "tests pass"):
    return SupervisoryEvent(event_id=f"sup_{task_id}_{type_}", task_id=task_id,
                            subagent_id=f"sub_{task_id}", type=type_,
                            summary=summary, requires_action=False,
                            task_title="Fix login")


class WorkerTurnsReachTheBoss(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.runtime = FakeRuntime()
        self.conductor = FakeConductor()
        self.backend = PtyManagerBackend(self.runtime, self.home, self.home / "x.sock",
                                         python="py", repo_root=self.home,
                                         helper=fake_helper(self.home),
                                         connect_timeout=1)
        self.backend.attach_bridge(FakeBridge())
        # A Boss with one child, opened by a first turn.
        asyncio.run(self.backend.handle("hello", self.conductor))
        self.runtime.sent.clear()
        self.backend.session.child_subagent_ids.append("sub_task_1")

    def tearDown(self):
        self.tmp.cleanup()

    def sent_text(self) -> str:
        return " ".join(m for _, m in self.runtime.sent)

    def test_a_child_workers_turn_is_typed_into_the_boss(self):
        async def scenario():
            taken = self.backend.deliver_supervisory(supervisory("task_1"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return taken
        self.assertTrue(asyncio.run(scenario()))
        self.assertIn("Your worker · Fix login (task_1) finished a turn: tests pass",
                      self.sent_text())
        events = self.backend.store.events(self.backend.session.id)
        kinds = [(e.type, e.payload.get("source") or e.payload.get("kind"))
                 for e in events]
        self.assertIn(("system_event", "worker_update"), kinds)
        self.assertIn(("boss_message", "worker_update"), kinds,
                      "the Boss's reply to an update is marked as such, not as typed")

    def test_a_worker_from_an_earlier_boss_is_still_reported_and_labelled(self):
        """Measured: eleven Boss sessions in a day, and the workers of the
        previous ten were never mentioned to the current one. The user
        has one Boss; whose worker it is gets said, not filtered on."""
        async def scenario():
            taken = self.backend.deliver_supervisory(supervisory("task_9"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return taken
        self.assertTrue(asyncio.run(scenario()))
        self.assertIn("Worker (started before this chat) · Fix login (task_9)",
                      self.sent_text())
        self.assertNotIn("Your worker", self.sent_text())

    def test_updates_go_in_as_they_arrive_one_push_at_a_time(self):
        """They used to wait for every open voice turn to end - measured
        2026-08-30: three finishes held three minutes behind one turn
        that had hung open. The reply the user hears is still the
        Boss's answer to them, not worker news."""
        async def scenario():
            async def during_turn():
                await asyncio.sleep(0)
                self.backend.deliver_supervisory(supervisory("task_1", summary="one"))
                self.backend.deliver_supervisory(supervisory("task_1", "failed", "two"))
            asyncio.get_event_loop().create_task(during_turn())
            turn = await self.backend.handle("how is it going?", self.conductor)
            for _ in range(4):
                await asyncio.sleep(0)
            return turn
        turn = asyncio.run(scenario())
        self.assertNotIn("Your worker", turn.reply)
        updates = [m for _, m in self.runtime.sent if m.startswith("Your worker")]
        self.assertEqual(len(updates), 1, "both went at once, as one typed line")
        self.assertIn("finished a turn: one · Your worker", updates[0])
        self.assertIn("failed: two", updates[0])

    def test_pushing_can_be_switched_off(self):
        self.backend.push_updates = False
        self.assertFalse(self.backend.deliver_supervisory(supervisory("task_1")))
        self.assertEqual(self.runtime.sent, [])


if __name__ == "__main__":
    unittest.main()


class WhatTheBossSaysBackIsHeard(WorkerTurnsReachTheBoss):
    def spoken(self) -> list[str]:
        return [c.args[0].data["text"] for c in self.conductor.bus.emit.call_args_list
                if c.args and c.args[0].type == "boss.tell_user"]

    def test_the_reply_to_a_pushed_update_is_spoken(self):
        """Measured: the Boss wrote "Here's what PR fifty-nine does..." back
        to an update and the user heard nothing."""
        async def scenario():
            self.backend.deliver_supervisory(supervisory("task_1", summary="PR 59 read."))
            for _ in range(4):
                await asyncio.sleep(0)
        asyncio.run(scenario())
        self.assertEqual(self.spoken(), ["Two tasks are open; Posely is running tests."])
        self.assertEqual(self.backend._pushes_open, 0)

    def test_a_reply_after_tell_user_is_not_said_twice(self):
        async def slow_send(session_id, message):
            self.runtime.sent.append((session_id, message))
            self.backend._told_user_in_push = True    # tell_user spoke already
            for handler in list(self.runtime.handlers.get(session_id, [])):
                handler(AgentEvent(type="completed", summary="Said it already."))
        self.runtime.send = slow_send
        async def scenario():
            self.backend.deliver_supervisory(supervisory("task_1"))
            for _ in range(4):
                await asyncio.sleep(0)
        asyncio.run(scenario())
        self.assertEqual(self.spoken(), [])

    def test_a_tool_only_turn_end_does_not_wedge_the_queue(self):
        """An empty turn end left a push marked open; every later update
        queued until the next spoken turn - "never arrived", measured."""
        async def quiet_send(session_id, message):
            self.runtime.sent.append((session_id, message))
            for handler in list(self.runtime.handlers.get(session_id, [])):
                handler(AgentEvent(type="completed", summary=""))
        self.runtime.send = quiet_send

        async def scenario():
            self.backend.deliver_supervisory(supervisory("task_1", summary="one"))
            for _ in range(4):
                await asyncio.sleep(0)
            self.assertEqual(self.backend._pushes_open, 0, "wedged")
            self.backend.deliver_supervisory(supervisory("task_1", "failed", "two"))
            for _ in range(4):
                await asyncio.sleep(0)
        asyncio.run(scenario())
        updates = [m for _, m in self.runtime.sent if m.startswith("Your worker")]
        self.assertEqual(len(updates), 2, "the second update waited for ever")

    def test_a_push_that_could_not_be_typed_is_tried_again(self):
        """Measured 22:31:25Z: "PTY is gone" twice during a dark wake;
        both updates - a finish among them - were logged and lost."""
        self.backend.PUSH_RETRY_S = (0.01,)
        real_send = self.runtime.send
        failures = {"left": 1}

        async def flaky_send(session_id, message):
            if failures["left"]:
                failures["left"] -= 1
                raise RuntimeError("cannot send: PTY is gone")
            await real_send(session_id, message)
        self.runtime.send = flaky_send

        async def scenario():
            self.backend.deliver_supervisory(supervisory("task_1", summary="one"))
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertEqual(self.backend._pushes_open, 0)
            self.assertEqual(len(self.backend._pending_updates), 1, "dropped")
            await asyncio.sleep(0.05)              # the retry fires
        asyncio.run(scenario())
        updates = [m for _, m in self.runtime.sent if m.startswith("Your worker")]
        self.assertEqual(len(updates), 1)
        self.assertIn("finished a turn: one", updates[0])
        self.assertEqual(self.backend._pending_updates, [])

    def test_a_push_past_its_retries_still_goes_with_the_next_flush(self):
        self.backend.PUSH_RETRY_S = ()
        async def dead_send(session_id, message):
            raise RuntimeError("cannot send: PTY is gone")
        real_send = self.runtime.send
        self.runtime.send = dead_send

        async def scenario():
            self.backend.deliver_supervisory(supervisory("task_1", summary="one"))
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertEqual(len(self.backend._pending_updates), 1)
            # The Boss is back; the next update takes the held one along.
            self.runtime.send = real_send
            self.backend.deliver_supervisory(supervisory("task_1", "failed", "two"))
            for _ in range(4):
                await asyncio.sleep(0)
        asyncio.run(scenario())
        updates = [m for _, m in self.runtime.sent if m.startswith("Your worker")]
        self.assertEqual(len(updates), 1, "one line, both updates")
        self.assertIn("finished a turn: one · Your worker", updates[0])
        self.assertIn("failed: two", updates[0])

    def test_an_update_does_not_wait_for_an_open_push_turn(self):
        """A push whose turn never ends - the Boss's process died, its
        window was closed under it - used to hold every later update for
        up to 90s. Claude Code queues typed lines itself, so the next
        update is typed right away."""
        async def never_ends(session_id, message):
            self.runtime.sent.append((session_id, message))
        self.runtime.send = never_ends

        async def scenario():
            self.backend.deliver_supervisory(supervisory("task_1", summary="one"))
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertEqual(self.backend._pushes_open, 1)
            self.backend.deliver_supervisory(supervisory("task_1", "failed", "two"))
            for _ in range(3):
                await asyncio.sleep(0)
        asyncio.run(scenario())
        self.assertEqual(len([m for _, m in self.runtime.sent if m.startswith("Your worker")]), 2)

    def test_a_dead_boss_is_retried_until_a_push_lands(self):
        """Past the listed delays the retry keeps coming at the ceiling
        cadence; a finish is never left waiting for a flush trigger
        that may never come."""
        self.backend.PUSH_RETRY_S = ()
        self.backend.PUSH_RETRY_CEILING_S = 0.01
        real_send = self.runtime.send
        failures = {"left": 3}

        async def flaky_send(session_id, message):
            if failures["left"]:
                failures["left"] -= 1
                raise RuntimeError("cannot send: PTY is gone")
            await real_send(session_id, message)
        self.runtime.send = flaky_send

        async def scenario():
            self.backend.deliver_supervisory(supervisory("task_1", summary="one"))
            await asyncio.sleep(0.2)
        asyncio.run(scenario())
        updates = [m for _, m in self.runtime.sent if m.startswith("Your worker")]
        self.assertEqual(len(updates), 1)
        self.assertIn("finished a turn: one", updates[0])
        self.assertEqual(self.backend._pending_updates, [])

    def test_a_retry_that_finds_the_boss_busy_comes_back(self):
        """busy used to end the retries: the held update then waited for
        the turn's own flush, and a turn that never ends held it for
        ever."""
        from conductor.pty_manager import _VoiceTurn
        self.backend.PUSH_RETRY_CEILING_S = 0.01
        self.backend._pending_updates = ["Your worker · Fix login (task_1) "
                                         "finished a turn: one"]
        turn = _VoiceTurn(text="x", started=0.0)
        self.backend._turns.append(turn)

        async def scenario():
            await self.backend._retry_push()       # busy: reschedules itself
            self.assertEqual(len(self.backend._pending_updates), 1)
            self.backend._turns.remove(turn)
            await asyncio.sleep(0.05)              # the comeback fires
        asyncio.run(scenario())
        updates = [m for _, m in self.runtime.sent if m.startswith("Your worker")]
        self.assertEqual(len(updates), 1)
        self.assertEqual(self.backend._pending_updates, [])

    def test_what_a_dead_process_owed_goes_when_the_session_opens(self):
        """A finish inboxed and lost with the process that held it is
        pushed by the next session open: the inbox is durable and an
        event is acked only when a push that carries it lands."""
        from conductor.supervisor_inbox import SupervisorInbox
        inbox = SupervisorInbox(self.home / "supervisor_inbox.jsonl")
        inbox.offer(supervisory("task_1", summary="tests pass"))
        conductor = FakeConductor()
        conductor.inbox = inbox
        runtime2 = FakeRuntime()
        backend2 = PtyManagerBackend(runtime2, self.home, self.home / "y.sock",
                                     python="py", repo_root=self.home,
                                     helper=fake_helper(self.home),
                                     connect_timeout=1)
        backend2.attach_bridge(FakeBridge())

        async def scenario():
            await backend2.handle("hello", conductor)
            for _ in range(6):
                await asyncio.sleep(0)
        asyncio.run(scenario())
        text = " ".join(m for _, m in runtime2.sent)
        self.assertIn("finished a turn: tests pass", text)
        self.assertEqual(inbox.pending_for_manager(), [])

    def test_an_update_carries_the_whole_finish(self):
        # 2026-08-30 23:41:59Z: the finish that said "Fix: PR #110" reached
        # the Boss without it. The message is the Boss's to read whole;
        # what the user is shown is cut where it is shown.
        long = ("The suite passes. " * 120).strip() + " Opened PR #110."
        line = self.backend.update_line(supervisory("task_1", summary=long))
        self.assertTrue(line.endswith("The suite passes. Opened PR #110."),
                        line[-60:])
        self.assertIn(long, line)
        self.assertEqual(whole_sentences("short", 10), "short")
        self.assertEqual(whole_sentences("no sentence end here at all " * 5, 40)[-1], "…")
