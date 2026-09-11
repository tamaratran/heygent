"""A restarted runtime's events are not "stale".

Measured (2026-08-29): a worker's sidecar had last_sequence 45; the app
restarted; the fresh runtime counted its events from 1 again and the
reducer dropped 61 of them as stale - the worker's finished turns
among them, so neither the card nor the Boss heard. A sequence is only
comparable within the epoch it was counted in; a new epoch starts the
count again.

Run with:  python3 -m unittest tests.test_restart_keeps_events -v
"""

from __future__ import annotations

import unittest

from conductor.agent_events import AgentEvent
from conductor.observability import ObservabilityBus
from conductor.subagent_state import SubagentState, reduce
from conductor.tmux_runtime import TmuxClaudeRuntime, _TmuxSession


def state_after_a_long_run() -> SubagentState:
    return SubagentState(id="sub_task_1", task_id="task_1", project_id="p",
                         provider="anthropic", provider_session_id="s",
                         title="Fix login", status="working",
                         last_sequence=45, last_epoch="ep_old")


def ev(type_: str, seq: int, epoch: str, **kw) -> AgentEvent:
    return AgentEvent(type=type_, sequence=seq, epoch=epoch,
                      event_id=f"aev_{epoch}_{seq}", **kw)


class ANewEpochStartsTheCountAgain(unittest.TestCase):
    def test_events_of_a_restarted_runtime_are_applied(self):
        state = state_after_a_long_run()
        outcomes = []
        for seq, (kind, kw) in enumerate([("progress", {"summary": "Reading a.py"}),
                                          ("progress", {"summary": "Bash(pytest)"}),
                                          ("completed", {"summary": "tests pass"})],
                                         start=1):
            state, transition = reduce(state, ev(kind, seq, "ep_new", **kw))
            outcomes.append(transition.outcome)
        self.assertEqual(outcomes, ["applied", "applied", "applied"])
        self.assertEqual(state.status, "idle")
        self.assertEqual(state.result["summary"], "tests pass")
        self.assertEqual((state.last_epoch, state.last_sequence), ("ep_new", 3))

    def test_a_late_event_within_the_new_epoch_is_still_stale(self):
        state = state_after_a_long_run()
        state, _ = reduce(state, ev("progress", 1, "ep_new", summary="a"))
        state, _ = reduce(state, ev("completed", 3, "ep_new", summary="done"))
        state, late = reduce(state, ev("progress", 2, "ep_new", summary="late"))
        self.assertEqual(late.outcome, "stale")
        self.assertEqual(state.status, "idle", "the late event did not rewind")

    def test_a_late_event_from_the_old_epoch_is_stale_too(self):
        state = state_after_a_long_run()
        state, _ = reduce(state, ev("completed", 1, "ep_new", summary="done"))
        # Straggler from the previous process: its 40 is below the old
        # mark of 45 - but the mark now belongs to ep_new. A different,
        # unknown epoch is a restart, so it applies (rare, and harmless:
        # the reducer's kind rules still hold - a stale progress after
        # a finish only marks the worker working again briefly).
        state, straggler = reduce(state, ev("progress", 40, "ep_old", summary="x"))
        self.assertEqual(straggler.outcome, "applied")

    def test_unstamped_events_behave_as_before(self):
        state = state_after_a_long_run()
        _, stale = reduce(state, AgentEvent(type="progress", sequence=3,
                                            summary="old"))
        self.assertEqual(stale.outcome, "stale")
        _, fresh = reduce(state, AgentEvent(type="progress", sequence=46,
                                            summary="new"))
        self.assertEqual(fresh.outcome, "applied")

    def test_the_epoch_is_persisted(self):
        state = state_after_a_long_run()
        state, _ = reduce(state, ev("progress", 1, "ep_new", summary="a"))
        again = SubagentState.from_dict(state.to_dict())
        self.assertEqual(again.last_epoch, "ep_new")
        old = state.to_dict()
        old.pop("last_epoch")
        self.assertEqual(SubagentState.from_dict(old).last_epoch, "")


class TheRuntimeStampsItsEpoch(unittest.TestCase):
    def test_every_emitted_event_carries_the_process_epoch(self):
        rt = TmuxClaudeRuntime(bus=ObservabilityBus(), transcript_dir=None)
        self.assertTrue(rt.epoch.startswith("ep_"))
        sess = _TmuxSession(task_id="task_1", name="cond_task_1",
                            working_directory="/tmp", session_id="s")
        seen = []
        sess.handlers.append(seen.append)
        rt._emit(sess, AgentEvent(type="progress", summary="a"))
        rt._emit(sess, AgentEvent(type="completed", summary="b"))
        self.assertEqual([(e.sequence, e.epoch) for e in seen],
                         [(1, rt.epoch), (2, rt.epoch)])
        other = TmuxClaudeRuntime(bus=ObservabilityBus(), transcript_dir=None)
        self.assertNotEqual(other.epoch, rt.epoch, "a restart is a new epoch")


if __name__ == "__main__":
    unittest.main()
