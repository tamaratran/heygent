"""The Fn key debounce: a flag that flickers is not a release.

macOS dropped the Fn bit for under a millisecond while the key was still
physically down, five times in one recorded evening. Each one closed the
microphone in the middle of a sentence, and none of them left a trace -
feeding silence to a released key is the designed path, so a release
nobody made looks exactly like a user who stopped talking.

The gate is driven here the way main() drives it: edges from the event tap,
and a 25 ms poll of the real hardware state in between.

The same key carries one gesture as well as the microphone: two quick taps
show and hide the overlay, and DoubleTapTest is about everything that must
not be mistaken for them.

Run with:  python3 -m unittest tests.test_hotkey -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hotkey import (FN_MASK, KEY_LABELS, KEY_MASKS, MAX_HOLD_S,
                    NUMPAD_MASK, POLL_INTERVAL_S, RELEASE_DEBOUNCE_S,
                    TAP_GAP_MAX_S, TAP_MAX_S, DoubleTap, FnGate,
                    chosen_key, fn_held, key_held, save_key)


class ReadingTheFlagsTest(unittest.TestCase):
    """The Fn bit alone is not the Fn key: Apple's keypad keys carry it too."""

    def test_the_fn_key_alone(self) -> None:
        self.assertTrue(fn_held(FN_MASK))

    def test_fn_with_an_ordinary_modifier_is_still_fn(self) -> None:
        shift = 0x20000
        self.assertTrue(fn_held(FN_MASK | shift))

    def test_an_arrow_key_is_not_fn(self) -> None:
        """Measured: an arrow held to scroll a log opened the microphone
        for 3.7 s and a single arrow tap was a 0.0 ms hold. Arrow, Home,
        End, Page and forward Delete all report Fn + NumericPad."""
        self.assertFalse(fn_held(FN_MASK | NUMPAD_MASK))

    def test_the_keypad_bit_alone_is_nothing(self) -> None:
        self.assertFalse(fn_held(NUMPAD_MASK))

    def test_no_flags_is_nothing(self) -> None:
        self.assertFalse(fn_held(0))


class ChoosingTheKeyTest(unittest.TestCase):
    """The push-to-talk key is whichever modifier the user picked."""

    def test_fn_keeps_the_keypad_rule(self) -> None:
        self.assertTrue(key_held(FN_MASK, "fn"))
        self.assertFalse(key_held(FN_MASK | NUMPAD_MASK, "fn"))

    def test_another_modifier_reads_its_own_bit(self) -> None:
        for name, mask in KEY_MASKS.items():
            if name == "fn":
                continue
            self.assertTrue(key_held(mask, name))
            self.assertFalse(key_held(0, name))

    def test_the_choice_round_trips_through_the_config(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "hotkey.json"
            self.assertEqual(chosen_key(path), "fn")   # nothing saved yet
            save_key("control", path)
            self.assertEqual(chosen_key(path), "control")

    def test_a_bad_config_falls_back_to_fn(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "hotkey.json"
            path.write_text('{"key": "caps_lock"}')
            self.assertEqual(chosen_key(path), "fn")
            path.write_text("not json")
            self.assertEqual(chosen_key(path), "fn")

    def test_an_unsupported_key_is_never_saved(self) -> None:
        with self.assertRaises(ValueError):
            save_key("caps_lock", "/dev/null")


SETTLE = RELEASE_DEBOUNCE_S * 4        # comfortably past the debounce


class FnGateTest(unittest.TestCase):

    def poll(self, gate: FnGate, start: float, down: bool | None,
             seconds: float = SETTLE) -> list:
        """Run the hardware poll, collecting whatever it decides to report.

        `down` is what the hardware says; None stands for a poll that could
        not read it and only lets the clock run.
        """
        said, now, end = [], start, start + seconds
        while now < end:
            now += POLL_INTERVAL_S
            state = gate.tick(now) if down is None else gate.confirm(down, now)
            if state is not None:
                said.append(state)
        return said

    def test_press_is_reported_immediately(self) -> None:
        """The press is the half the user can feel; it is not debounced."""
        gate = FnGate()
        self.assertIs(gate.observe(True, 0.0), True)

    def test_a_real_hold_and_release_is_one_pair(self) -> None:
        gate = FnGate()
        self.assertIs(gate.observe(True, 0.0), True)
        self.assertIsNone(gate.observe(False, 3.0))
        self.assertEqual(self.poll(gate, 3.0, False), [False])

    def test_nothing_is_said_inside_the_debounce_window(self) -> None:
        """The key may still come back, so the microphone stays open."""
        gate = FnGate()
        gate.observe(True, 0.0)
        gate.observe(False, 1.0)
        self.assertEqual(
            self.poll(gate, 1.0, False, seconds=RELEASE_DEBOUNCE_S / 2), [])

    def test_a_flicker_never_reaches_the_microphone(self) -> None:
        """The bug: the flag drops out and comes straight back.

        Not "released then pressed again" - that would still have shut the
        microphone for the gap. One unbroken hold, and nothing said at all.
        """
        gate = FnGate()
        self.assertIs(gate.observe(True, 0.0), True)
        self.assertIsNone(gate.observe(False, 1.000))    # the flag drops out
        self.assertIsNone(gate.observe(True, 1.001))     # ... and returns
        self.assertEqual(self.poll(gate, 1.001, True), [])
        self.assertTrue(gate.held, "the hold must survive the flicker")

    def test_the_recorded_failure(self) -> None:
        """Replayed from conductor-*.jsonl, 2026-08-28T06:10:58Z.

        Two "holds" of 2.9 ms and 0.6 ms, back to back, while the user was
        part-way through asking for tests to be run on PR 22. Nothing was
        transcribed, and their next words were "Did you hear that I said I
        wanted you to run tests on PR 22". The key never came up.
        """
        gate = FnGate()
        said = []
        for now, down in [(0.0000, True), (0.0029, False), (0.0030, True),
                          (0.0036, False), (0.0037, True)]:
            state = gate.observe(down, now)
            if state is not None:
                said.append(state)
        self.assertEqual(said, [True], "the blips must not close the mic")

        # The user keeps talking for twenty seconds with the key down.
        self.assertEqual(self.poll(gate, 0.0037, True, seconds=20.0), [])
        self.assertTrue(gate.held)

        # Only when they really let go does the utterance end.
        self.assertIsNone(gate.observe(False, 20.0))
        self.assertEqual(self.poll(gate, 20.0, False), [False])

    def test_the_poll_alone_matures_a_release(self) -> None:
        """The tap speaks only on edges. Without the poll driving it, a
        release that gets no second edge would never be reported."""
        gate = FnGate()
        gate.observe(True, 0.0)
        gate.observe(False, 1.0)
        self.assertEqual(self.poll(gate, 1.0, None), [False])

    def test_a_release_the_tap_missed_is_caught_once_the_poll_is_proven(
            self) -> None:
        """A tap that dies mid-hold leaves the key looking held for ever.

        The poll can let go without it - but only after it has seen the key
        down for itself, which is what proves it can see Fn at all.
        """
        gate = FnGate()
        gate.observe(True, 0.0)
        self.assertEqual(self.poll(gate, 0.0, True, seconds=0.2), [])
        self.assertEqual(self.poll(gate, 5.0, False), [False])

    def test_a_poll_that_cannot_see_the_key_never_ends_a_hold(self) -> None:
        """Not every Mac reports Fn in the hardware flags. One that does not
        reads as a key permanently up, and must not be allowed to say so -
        that would be a worse bug than the one being fixed.

        Inside the ceiling, that is: past MAX_HOLD_S the hold ends anyway,
        for a different reason and by a different rule
        (AHoldHasACeilingTest). Held here to a length a finger can make.
        """
        gate = FnGate()
        self.assertIs(gate.observe(True, 0.0), True)     # tap saw the press
        self.assertEqual(self.poll(gate, 0.0, False, seconds=20.0), [],
                         "a blind poll must not release the key")
        self.assertTrue(gate.held)
        # The tap is still in charge, and a real release still works.
        self.assertIsNone(gate.observe(False, 20.0))
        self.assertEqual(self.poll(gate, 20.0, False), [False])

    def test_a_blind_poll_still_lets_the_flicker_fix_work(self) -> None:
        """The debounce is the half that matters, and it needs no poll."""
        gate = FnGate()
        gate.observe(True, 0.0)
        gate.observe(False, 1.000)                       # the flag drops out
        gate.observe(True, 1.001)                        # ... and returns
        self.assertEqual(self.poll(gate, 1.001, False, seconds=5.0), [])
        self.assertTrue(gate.held)

    def test_never_reports_the_same_state_twice(self) -> None:
        gate = FnGate()
        self.assertIs(gate.observe(True, 0.0), True)
        self.assertEqual(self.poll(gate, 0.0, True), [])
        gate.observe(False, 1.0)
        self.assertEqual(self.poll(gate, 1.0, False, seconds=5.0), [False])


class DoubleTapTest(unittest.TestCase):
    """Two quick taps show and hide the overlay. Nothing else may.

    The same key is push-to-talk, so the cost of a false positive is the
    notifications appearing or vanishing in the middle of a sentence the user is
    saying - and the cost of a false negative is only that they tap again.
    These tests are almost all about what must NOT count.
    """

    def taps(self, presses, tap=DoubleTap) -> list[float]:
        """Feed (down_at, up_at) presses; return when a gesture completed."""
        gesture, fired = tap(), []
        for down_at, up_at in presses:
            gesture.observe(True, down_at)
            if gesture.observe(False, up_at):
                fired.append(up_at)
        return fired

    def test_two_quick_taps_are_a_gesture(self) -> None:
        self.assertEqual(len(self.taps([(0.0, 0.08), (0.20, 0.28)])), 1)

    def test_it_fires_on_the_second_release(self) -> None:
        """Not on the second press: a press that turns into a hold is the
        user starting to talk, and must not have toggled anything first."""
        self.assertEqual(self.taps([(0.0, 0.08), (0.20, 0.28)]), [0.28])

    def test_one_tap_is_nothing(self) -> None:
        self.assertEqual(self.taps([(0.0, 0.08)]), [])

    def test_two_holds_in_a_row_are_two_utterances(self) -> None:
        """The gesture a user makes by accident: talk, let go, talk again.

        Both presses are far too long to be taps, so however little time
        there is between them, this is someone speaking twice.
        """
        self.assertEqual(self.taps([(0.0, 2.0), (2.05, 4.0)]), [])

    def test_a_tap_then_a_hold_is_not_a_gesture(self) -> None:
        self.assertEqual(self.taps([(0.0, 0.08), (0.20, 2.0)]), [])

    def test_a_hold_then_a_tap_is_not_a_gesture(self) -> None:
        self.assertEqual(self.taps([(0.0, 2.0), (2.05, 2.13)]), [])

    def test_a_hold_clears_a_tap_that_came_before_it(self) -> None:
        """A tap, then a hold, then a tap. The two taps are not a pair:
        the user spoke in between, so the second tap starts over."""
        self.assertEqual(
            self.taps([(0.0, 0.08), (0.20, 2.0), (2.05, 2.13)]), [])

    def test_taps_too_far_apart_are_two_taps(self) -> None:
        late = TAP_GAP_MAX_S + 0.05
        self.assertEqual(self.taps([(0.0, 0.08), (0.08 + late, 0.16 + late)]),
                         [])

    def test_a_press_longer_than_a_tap_is_a_hold(self) -> None:
        long = TAP_MAX_S + 0.05
        self.assertEqual(self.taps([(0.0, long), (long + 0.05, long + 0.13)]),
                         [])

    def test_three_taps_toggle_once(self) -> None:
        """Not twice, and not zero times: a third tap begins a new gesture.

        Toggling twice would leave the overlay exactly where it started,
        which is indistinguishable from the gesture not working at all.
        """
        self.assertEqual(
            len(self.taps([(0.0, 0.08), (0.16, 0.24), (0.32, 0.40)])), 1)

    def test_four_taps_toggle_twice(self) -> None:
        self.assertEqual(
            len(self.taps([(0.0, 0.08), (0.16, 0.24),
                           (0.32, 0.40), (0.48, 0.56)])), 2)

    def test_a_release_with_no_press_behind_it_is_ignored(self) -> None:
        """The poll can mature a release for a press this never saw - at
        startup, with the key already down."""
        gesture = DoubleTap()
        self.assertFalse(gesture.observe(False, 0.0))
        self.assertFalse(gesture.observe(False, 0.1))

    def test_a_real_double_tap_through_the_debounce(self) -> None:
        """End to end on the stream a consumer actually sees.

        DoubleTap reads the reported events, not the raw ones, so every
        release reaches it RELEASE_DEBOUNCE_S late: a 90 ms tap measures
        150 ms here and a 120 ms gap measures 60 ms. The thresholds have
        to hold for the numbers a finger really produces, after that
        shift - which is the whole reason this test drives FnGate too.
        """
        gate, gesture, fired = FnGate(), DoubleTap(), []
        now = 0.0
        for _ in range(2):
            for down, seconds in ((True, 0.090), (False, 0.120)):
                state = gate.observe(down, now)
                if state is not None:
                    fired.append(gesture.observe(state, now))
                end = now + seconds
                while now < end:                 # the poll, in between
                    now += POLL_INTERVAL_S
                    state = gate.confirm(down, now)
                    if state is not None:
                        fired.append(gesture.observe(state, now))
        self.assertEqual(fired.count(True), 1, f"gesture missed: {fired}")


class ThePollMayNotOpenTheMicrophoneTest(unittest.TestCase):
    """A press has one witness: the tap.

    The poll polls; it does not witness. A press it originates opens the
    microphone with nothing downstream able to tell it from one the user
    made, and if the state that produced it is wrong there is no edge
    coming to close it either.
    """

    def test_a_poll_alone_never_starts_a_hold(self) -> None:
        gate = FnGate()
        said = []
        now = 0.0
        for _ in range(400):                  # ten seconds of a latched bit
            now += POLL_INTERVAL_S
            state = gate.confirm(True, now)
            if state is not None:
                said.append(state)
        self.assertEqual(said, [], "the poll must not invent a press")
        self.assertFalse(gate.held)

    def test_the_tap_still_opens_it(self) -> None:
        """The rule is about provenance, not about refusing presses."""
        gate = FnGate()
        self.assertIs(gate.observe(True, 0.0), True)
        self.assertTrue(gate.held)

    def test_the_poll_still_kills_a_phantom_release(self) -> None:
        """The reason the poll is trusted on `down` at all, unchanged: it
        may keep a hold alive, it just may not begin one."""
        gate = FnGate()
        gate.observe(True, 0.0)
        gate.observe(False, 1.000)                       # the flag drops out
        self.assertIsNone(gate.confirm(True, 1.001))     # the poll says otherwise
        self.assertEqual(self.poll_for(gate, 1.001, True, 5.0), [])
        self.assertTrue(gate.held, "the hold must survive the flicker")

    def test_a_press_the_poll_saw_still_proves_it_can_see_fn(self) -> None:
        """Ignoring the press for the gate must not cost the poll its
        licence to end a hold - that licence is what catches a dead tap."""
        gate = FnGate()
        self.assertIsNone(gate.confirm(True, 0.0))       # ignored, but noted
        gate.observe(True, 1.0)                          # a real press
        self.assertEqual(self.poll_for(gate, 5.0, False, SETTLE), [False])

    def poll_for(self, gate, start, down, seconds):
        said, now, end = [], start, start + seconds
        while now < end:
            now += POLL_INTERVAL_S
            state = gate.confirm(down, now)
            if state is not None:
                said.append(state)
        return said


class AHoldHasACeilingTest(unittest.TestCase):
    """Nothing else here reasons about how long the key has been down.

    Replayed from conductor-91007.jsonl, 2026-09-03T02:23:07Z: the gate
    opened and stayed open for 258.3 s - `listening_stopped` with
    duration_ms 258275.5 - while two utterances of the room's own
    conversation were transcribed and sent on as requests. Every hold the
    user actually made that session lasted between 1.3 s and 12.0 s.

    This is the half that holds whatever went wrong upstream: a press
    nobody made, or a release that was missed. Neither is decidable from
    the log, and the ceiling does not need to decide.
    """

    def poll(self, gate: FnGate, start: float, down: bool | None,
             seconds: float) -> list:
        said, now, end = [], start, start + seconds
        while now < end:
            now += POLL_INTERVAL_S
            state = gate.tick(now) if down is None else gate.confirm(down, now)
            if state is not None:
                said.append(state)
        return said

    def test_a_stuck_key_is_let_go_of(self) -> None:
        gate = FnGate()
        self.assertIs(gate.observe(True, 0.0), True)
        said = self.poll(gate, 0.0, True, MAX_HOLD_S + 1.0)
        self.assertEqual(said, [False], "the ceiling must end the hold")
        self.assertFalse(gate.held)
        self.assertTrue(gate.capped, "and say it was the ceiling, not a finger")

    def test_the_ceiling_is_reached_with_no_evidence_at_all(self) -> None:
        """A blind poll cannot read the key, so it cannot see the stuck
        bit either. Time still passes, and the ceiling is about time."""
        gate = FnGate()
        gate.observe(True, 0.0)
        self.assertEqual(self.poll(gate, 0.0, None, MAX_HOLD_S + 1.0), [False])

    def test_a_real_hold_is_never_cut(self) -> None:
        """The longest hold measured in that session was 12.0 s."""
        gate = FnGate()
        gate.observe(True, 0.0)
        self.assertEqual(self.poll(gate, 0.0, True, 12.0), [])
        self.assertTrue(gate.held)
        self.assertFalse(gate.capped)

    def test_the_poll_cannot_hold_the_ceiling_off(self) -> None:
        """The stuck poll re-asserted the press every 25 ms. If that
        restarted the clock the ceiling could never be reached - which is
        why only a newly reported press stamps it."""
        gate = FnGate()
        gate.observe(True, 0.0)
        said = self.poll(gate, 0.0, True, MAX_HOLD_S * 2)
        self.assertEqual(said, [False])

    def test_a_flicker_does_not_restart_the_clock_either(self) -> None:
        gate = FnGate()
        gate.observe(True, 0.0)
        for at in (5.0, 10.0, 15.0, 20.0):
            gate.observe(False, at)          # the flag drops out ...
            gate.observe(True, at + 0.001)   # ... and comes back
        self.assertEqual(self.poll(gate, 20.001, True, MAX_HOLD_S), [False])

    def test_the_key_works_again_afterwards(self) -> None:
        """A capped release is an ordinary release: the next real press
        opens the microphone as usual."""
        gate = FnGate()
        gate.observe(True, 0.0)
        self.poll(gate, 0.0, True, MAX_HOLD_S + 1.0)
        self.assertIs(gate.observe(True, 100.0), True)
        self.assertTrue(gate.held)
        self.assertFalse(gate.capped, "a finger this time")

    def test_the_recorded_stuck_hold(self) -> None:
        """The 258 s hold on the reading where the press was never made:
        a poll-sourced press, then nothing but the poll agreeing with
        itself until 02:27:25Z. On the other reading - a real press whose
        release went missing - the ceiling above is what ends it."""
        gate = FnGate()
        said = []
        now = 0.0
        while now < 258.3:
            now += POLL_INTERVAL_S
            state = gate.confirm(True, now)
            if state is not None:
                said.append(state)
        self.assertEqual(said, [], "it should never have opened")
        self.assertFalse(gate.held)


if __name__ == "__main__":
    unittest.main()
