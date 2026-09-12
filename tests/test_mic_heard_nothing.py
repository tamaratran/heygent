"""A microphone that hears nothing is said, not inferred from silence.

A Mac with heygent's Microphone switch off fails nowhere: CoreAudio opens
the input and delivers exact zeros, Fn works, the capsule shows, and the
transcript simply never comes. The only trace was a user saying "the
voice isn't transcribing". Now a hold of a second or more with not one
non-zero sample is logged as voice.mic_heard_nothing and, once per run,
said to the user with the cause macOS reports.

Run with:  python3 -m unittest tests.test_mic_heard_nothing -v
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

import numpy as np

import voice_agent
from voice_agent import VoiceAgent


def agent():
    a = VoiceAgent.__new__(VoiceAgent)
    a.ws = None
    a.running = True
    a.holding = False
    a.session_told_holding = False
    a.key_moved_at = 0.0
    a.held_at = 0.0
    a.released_at = 0.0
    a.awaiting_first_audio = False
    a.trace_id = ""
    a.mic_device = "MacBook Pro Microphone"
    a.mic_level = 0.0
    a.hold_heard = False
    a.hold_blocks = 0
    a.silence_told = False
    a.silence_task = None
    a.audio_recovering = False
    a.ask_settle = None
    a.reply_settle = None
    a.mic_q = asyncio.Queue()
    a.speaker = mock.Mock(speaking=False)
    a._emit = lambda *args, **kwargs: None
    a._arm_ask = lambda: None
    return a


class TheHoldRemembersWhetherAnythingArrived(unittest.TestCase):
    def test_zeros_leave_the_hold_unheard(self) -> None:
        a = agent()
        a.holding = True
        cb = a._mic_callback(mock.Mock())
        cb(np.zeros(480, dtype=np.int16), 480, None, None)
        self.assertFalse(a.hold_heard)

    def test_one_non_zero_sample_is_a_live_microphone(self) -> None:
        a = agent()
        a.holding = True
        cb = a._mic_callback(mock.Mock())
        block = np.zeros(480, dtype=np.int16)
        block[7] = 3            # room tone, far below the level meter
        cb(block, 480, None, None)
        self.assertTrue(a.hold_heard)

    def test_a_new_hold_starts_unheard(self) -> None:
        a = agent()
        a.hold_heard = True
        asyncio.run(a.set_holding(True))
        self.assertFalse(a.hold_heard)


class ASilentHoldIsSaid(unittest.TestCase):
    def release_after(self, a, held_s: float, heard: bool,
                      blocks: int = 40):
        told = []

        async def go():
            await a.set_holding(True)
            a.hold_heard = heard
            a.hold_blocks = blocks
            a.held_at = time.monotonic() - held_s
            with mock.patch.object(voice_agent, "probe_voice_grants",
                                   lambda w: {"microphone": False}), \
                    mock.patch.object(voice_agent.dialogs, "tell",
                                      told.append), \
                    mock.patch.object(voice_agent, "application_log") as log:
                a.silence_task = None
                await a.set_holding(False)
                if a.silence_task is not None:
                    await a.silence_task
                return log
        log = asyncio.run(go())
        return told, log

    def test_a_second_of_zeros_is_logged_and_said(self) -> None:
        a = agent()
        told, log = self.release_after(a, 1.5, heard=False)
        events = [c.args[1] for c in log.call_args_list]
        self.assertIn("voice.mic_heard_nothing", events)
        self.assertEqual(len(told), 1)
        self.assertIn("heard nothing", told[0])
        self.assertIn("MacBook Pro Microphone", told[0])
        self.assertIn("Privacy & Security > Microphone", told[0])
        self.assertTrue(a.silence_told)

    def test_said_once_per_run(self) -> None:
        a = agent()
        self.release_after(a, 1.5, heard=False)
        told, log = self.release_after(a, 1.5, heard=False)
        events = [c.args[1] for c in log.call_args_list]
        self.assertIn("voice.mic_heard_nothing", events, "still logged")
        self.assertEqual(told, [], "but not said again")

    def test_a_short_tap_is_not_a_dead_microphone(self) -> None:
        a = agent()
        told, log = self.release_after(a, 0.5, heard=False)
        events = [c.args[1] for c in log.call_args_list]
        self.assertNotIn("voice.mic_heard_nothing", events)
        self.assertEqual(told, [])

    def test_no_blocks_at_all_is_the_stalled_stream_not_silence(
            self) -> None:
        """A stream that delivered nothing during the hold is _watch_audio's
        stall (audio.recover_*), not a microphone that hears zeros."""
        a = agent()
        told, log = self.release_after(a, 2.0, heard=False, blocks=0)
        events = [c.args[1] for c in log.call_args_list]
        self.assertNotIn("voice.mic_heard_nothing", events)
        self.assertEqual(told, [])

    def test_a_hold_that_heard_something_is_fine(self) -> None:
        a = agent()
        told, log = self.release_after(a, 3.0, heard=True)
        events = [c.args[1] for c in log.call_args_list]
        self.assertNotIn("voice.mic_heard_nothing", events)
        self.assertEqual(told, [])

    def test_a_granted_microphone_points_at_sound_input(self) -> None:
        a = agent()
        told = []

        async def go():
            await a.set_holding(True)
            a.hold_blocks = 40
            a.held_at = time.monotonic() - 2.0
            with mock.patch.object(voice_agent, "probe_voice_grants",
                                   lambda w: {"microphone": True}), \
                    mock.patch.object(voice_agent.dialogs, "tell",
                                      told.append), \
                    mock.patch.object(voice_agent, "application_log"):
                await a.set_holding(False)
                self.assertIsNotNone(a.silence_task)
                await a.silence_task
        asyncio.run(go())
        self.assertEqual(len(told), 1)
        self.assertIn("Sound > Input", told[0])
        self.assertNotIn("macOS is not letting", told[0])


if __name__ == "__main__":
    unittest.main()
