"""Switching microphones, or losing the AirPods, must not end the session.

What actually happened, in a live run: the AirPods disconnected, PortAudio
printed `||PaMacCore (AUHAL)|| Error on line 2523: err='-50'`, and the
session stayed open with a microphone that heard nothing. Nothing raised.
The stream simply stopped calling its callback, so the only evidence was
the agent no longer answering - "it feels like it cant hear us anymore".

Both streams are bound to whatever device was default when they opened, so
the fix is to notice the silence and rebuild them on the current device
while keeping the WebSocket: the conversation survives a device change,
only the streams are rebuilt.

Run with:  python3 -m unittest tests.test_audio_devices -v
"""

from __future__ import annotations

import asyncio
import re
import time
import unittest
from unittest import mock

import voice_agent
from voice_agent import VoiceAgent


DEVICES = [
    {"name": "Tamara's AirPods", "max_input_channels": 1},
    {"name": "Tamara's AirPods", "max_input_channels": 0},
    {"name": "MacBook Pro Microphone", "max_input_channels": 1},
    {"name": "MacBook Pro Speakers", "max_input_channels": 0},
]


class TheMicThatKeepsTheMusic(unittest.TestCase):
    """Holding a Bluetooth headset's microphone drops the whole headset
    from A2DP to HFP for as long as the stream is open - "my music
    became lower quality with headphones" (reported 2026-09-01). The
    built-in microphone is held instead; the headphones keep their
    music profile."""

    def test_the_built_in_mic_beats_a_bluetooth_default(self):
        with mock.patch.object(voice_agent.sd, "query_devices",
                               return_value=DEVICES):
            with mock.patch.dict("os.environ", {"VOICE_AGENT_MIC": ""}):
                self.assertEqual(voice_agent._preferred_input(), 2)

    def test_the_override_names_its_own_device(self):
        with mock.patch.object(voice_agent.sd, "query_devices",
                               return_value=DEVICES):
            with mock.patch.dict("os.environ",
                                 {"VOICE_AGENT_MIC": "airpods"}):
                self.assertEqual(voice_agent._preferred_input(), 0)
            with mock.patch.dict("os.environ",
                                 {"VOICE_AGENT_MIC": "default"}):
                self.assertIsNone(voice_agent._preferred_input())

    def test_no_built_in_falls_back_to_the_default(self):
        externals = [{"name": "USB Interface", "max_input_channels": 2}]
        with mock.patch.object(voice_agent.sd, "query_devices",
                               return_value=externals):
            with mock.patch.dict("os.environ", {"VOICE_AGENT_MIC": ""}):
                self.assertIsNone(voice_agent._preferred_input())


class FakeStream:
    def __init__(self, fail: bool = False) -> None:
        self.started = False
        self.stopped = False
        self.closed = False
        self.fail = fail

    def start(self) -> None:
        if self.fail:
            raise OSError("device unavailable")
        self.started = True

    def stop(self) -> None:
        if self.fail:
            raise OSError("device is gone")
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class FakeSpeaker:
    def __init__(self) -> None:
        self.stream = FakeStream()
        self.device_name = "MacBook Pro Speakers"
        self.reopened = 0

    def reopen(self) -> None:
        self.reopened += 1
        self.stream = FakeStream()


class Base(unittest.TestCase):
    def agent(self) -> VoiceAgent:
        agent = VoiceAgent.__new__(VoiceAgent)
        agent.running = True
        agent.audio_recovering = False
        agent.last_mic_at = time.monotonic()
        agent.mic = FakeStream()
        agent.mic_device = "Will's AirPods Pro"
        agent.mic_level = 0.0
        agent.holding = False
        agent.speaker = FakeSpeaker()
        agent.mic_q = asyncio.Queue(maxsize=200)
        self.opened: list[str] = []
        self.order: list[str] = []
        return agent

    def fake_sd(self, name="MacBook Pro Microphone", fail=False):
        sd = mock.Mock()
        sd.query_devices.return_value = {"name": name,
                                         "default_samplerate": 48000.0,
                                         "max_output_channels": 2}

        def make_stream(*_a, **_k):
            self.order.append("open")
            self.opened.append(name)
            if fail:
                raise OSError("no such device")
            return FakeStream()
        sd.InputStream.side_effect = make_stream
        return sd

    def run_recovery(self, agent, sd, silent_for=3.0):
        with mock.patch.object(voice_agent, "sd", sd), \
             mock.patch.object(voice_agent, "refresh_device_list",
                               lambda: self.order.append("refresh")), \
             mock.patch.object(voice_agent, "close_quietly",
                               lambda s: self.order.append("close")):
            asyncio.run(agent._recover_audio(None, silent_for))


class LosingTheDevice(Base):
    def test_a_stalled_microphone_is_reopened_on_the_current_device(self):
        agent = self.agent()
        self.run_recovery(agent, self.fake_sd())
        self.assertEqual(self.opened, ["MacBook Pro Microphone"])
        self.assertEqual(agent.mic_device, "MacBook Pro Microphone")
        self.assertEqual(agent.speaker.reopened, 1,
                         "playback stayed on the device that went away")

    def test_the_device_list_is_refreshed_only_once_streams_are_closed(self):
        """PortAudio caches the device list at initialisation and will not
        re-enumerate with streams open, so a process that was running when
        the AirPods left goes on offering them."""
        agent = self.agent()
        self.run_recovery(agent, self.fake_sd())
        self.assertEqual(self.order[:3], ["close", "close", "refresh"])
        self.assertIn("open", self.order[3:])

    def test_a_working_microphone_is_left_alone(self):
        """Recovery tears down live streams. Doing that to a healthy
        device would be the bug, not the fix."""
        agent = self.agent()
        recovered = []
        agent._recover_audio = lambda *a: recovered.append(a)
        agent._device_switched = lambda: ""      # nothing changed

        async def drive():
            task = asyncio.create_task(agent._watch_audio(None))
            for _ in range(3):
                agent.last_mic_at = time.monotonic()   # blocks arriving
                await asyncio.sleep(0.4)
            agent.running = False
            await asyncio.sleep(0.05)
            task.cancel()
        with mock.patch.object(VoiceAgent, "AUDIO_CHECK_SECONDS", 0.1):
            asyncio.run(drive())
        self.assertEqual(recovered, [])

    def test_silence_is_stalled_but_a_device_gap_is_not_the_end(self):
        """Between the headphones leaving and the built-in taking over
        there is a window with no usable device at all. Failing there must
        not kill the session - the next tick tries again."""
        agent = self.agent()
        self.run_recovery(agent, self.fake_sd(fail=True))
        self.assertTrue(agent.running, "a device gap ended the session")
        self.assertFalse(agent.audio_recovering, "recovery latched on")

    def test_a_failed_recovery_does_not_spin(self):
        agent = self.agent()
        agent.last_mic_at = 0.0
        self.run_recovery(agent, self.fake_sd(fail=True))
        self.assertGreater(agent.last_mic_at, 0.0,
                           "would retry on every single tick")


class SwitchingMicrophones(Base):
    """The other failure, and the quiet one. A device that DISAPPEARS
    stops the callbacks; a device the user switches AWAY from keeps
    delivering perfectly, from the wrong microphone. Only the second one
    is invisible to the stall check."""

    def test_a_switch_is_noticed_even_though_audio_still_flows(self):
        agent = self.agent()
        agent.last_mic_at = time.monotonic()          # blocks still arriving
        with mock.patch.object(voice_agent, "default_input_name",
                               lambda: "iPhone Microphone"), \
             mock.patch.object(voice_agent, "default_output_name",
                               lambda: agent.speaker.device_name):
            self.assertIn("iPhone Microphone", agent._device_switched())

    def test_a_switched_output_counts_too(self):
        agent = self.agent()
        with mock.patch.object(voice_agent, "default_input_name",
                               lambda: agent.mic_device), \
             mock.patch.object(voice_agent, "default_output_name",
                               lambda: "AirPods Pro"):
            self.assertIn("AirPods Pro", agent._device_switched())

    def test_an_unreadable_default_is_not_a_change(self):
        """CoreAudio failing must never tear down a working stream."""
        agent = self.agent()
        with mock.patch.object(voice_agent, "default_input_name", lambda: ""), \
             mock.patch.object(voice_agent, "default_output_name", lambda: ""):
            self.assertEqual(agent._device_switched(), "")

    def test_asking_twice_works_twice(self):
        """It did not. `import ctypes.util` inside the function made
        ctypes a local, so every call after the one that ran the import
        raised UnboundLocalError - swallowed into "", which reads as "no
        device change" forever. The bug hid behind its own fallback."""
        first = voice_agent.default_input_name()
        self.assertEqual(voice_agent.default_input_name(), first)
        self.assertEqual(voice_agent.default_output_name(),
                         voice_agent.default_output_name())
        if first:
            self.assertTrue(voice_agent.default_output_name(),
                            "output name empty while input resolves")

    def test_the_names_agree_with_portaudio(self):
        """They have to: a mismatch would read as a permanent device
        switch and reopen both streams every single tick."""
        core = voice_agent.default_input_name()
        if not core:
            self.skipTest("CoreAudio default not readable here")
        self.assertEqual(core,
                         voice_agent.sd.query_devices(kind="input")["name"])
        self.assertEqual(voice_agent.default_output_name(),
                         voice_agent.sd.query_devices(kind="output")["name"])

    def test_the_default_is_read_from_coreaudio_not_portaudio(self):
        """PortAudio caches the device list at initialisation, so it can
        report a device the user has already switched away from."""
        name = voice_agent.default_input_name()
        self.assertIsInstance(name, str)
        if name:
            self.assertIn(name, [d["name"] for d in voice_agent.sd.query_devices()
                                 if d["max_input_channels"]])


class TheSessionClockKeepsRunning(Base):
    def test_silence_is_fed_while_there_is_no_microphone(self):
        """The Live session runs on an audio clock: an inbound stream that
        stops entirely stops it responding, so the gap has to be filled."""
        agent = self.agent()
        agent._feed_silence()
        self.assertGreater(agent.mic_q.qsize(), 0)
        block = agent.mic_q.get_nowait()
        self.assertEqual(set(block), {0})

    def test_feeding_silence_cannot_wedge_on_a_full_queue(self):
        agent = self.agent()
        agent.mic_q = asyncio.Queue(maxsize=2)
        agent._feed_silence()                      # must not raise
        self.assertEqual(agent.mic_q.qsize(), 2)


class PlaybackFollowsTheDevice(unittest.TestCase):
    def test_rate_and_channels_are_read_per_open_not_once(self):
        """AirPods and the built-in speakers do not agree on either, and a
        stream built for the wrong one plays nothing."""
        sd = mock.Mock()
        sd.query_devices.return_value = {"name": "AirPods",
                                         "default_samplerate": 48000.0,
                                         "max_output_channels": 2}
        sd.OutputStream.return_value = FakeStream()
        with mock.patch.object(voice_agent, "sd", sd):
            speaker = voice_agent.Speaker()
            self.assertEqual((speaker.rate, speaker.channels), (48000, 2))
            sd.query_devices.return_value = {"name": "USB mono",
                                             "default_samplerate": 24000.0,
                                             "max_output_channels": 1}
            with mock.patch.object(voice_agent, "close_quietly", lambda s: None):
                speaker.reopen()
        self.assertEqual((speaker.rate, speaker.channels), (24000, 1))
        self.assertEqual(speaker.device_name, "USB mono")

    def test_queued_audio_survives_the_swap(self):
        """The reply being spoken when the headphones died should finish
        out loud, not be dropped."""
        sd = mock.Mock()
        sd.query_devices.return_value = {"name": "x",
                                         "default_samplerate": 48000.0,
                                         "max_output_channels": 2}
        sd.OutputStream.return_value = FakeStream()
        with mock.patch.object(voice_agent, "sd", sd), \
             mock.patch.object(voice_agent, "close_quietly", lambda s: None):
            speaker = voice_agent.Speaker()
            speaker.feed(b"\x01\x02" * 100)
            before = len(speaker.buffer)
            speaker.reopen()
        self.assertEqual(len(speaker.buffer), before)


class TheMicCallbackNeverRaisesIntoTheLoop(Base):
    def test_a_full_queue_drops_a_block_instead_of_raising(self):
        """put_nowait was scheduled onto the loop directly, so QueueFull
        was raised where the except around the scheduling could not catch
        it: an unhandled traceback per block while the drain was behind."""
        agent = self.agent()
        agent.mic_q = asyncio.Queue(maxsize=1)
        agent._enqueue_mic(b"\x00\x00")
        agent._enqueue_mic(b"\x00\x00")          # must not raise
        self.assertEqual(agent.mic_q.qsize(), 1)


class ClosingWhatIsActuallyOpen(unittest.TestCase):
    def test_close_quietly_survives_an_unplugged_device(self):
        """stop() and close() both raise once the device is gone, and by
        then there is nothing to salvage."""
        voice_agent.close_quietly(FakeStream(fail=True))     # must not raise

    def test_teardown_closes_the_stream_recovery_installed(self):
        """A local `mic` name captured at session start would stop a dead
        handle and leave the live device running."""
        import re
        from pathlib import Path
        source = Path(voice_agent.__file__).read_text()
        # Anchor on the statement, not on its indentation: the teardown
        # moved out of two nested `async with` blocks when the session
        # became a reconnect loop, and the property being checked did not.
        anchor = re.search(r"self\.running = False\n\s+for task", source)
        self.assertIsNotNone(anchor, "teardown block not found")
        teardown = source[anchor.start():]
        teardown = teardown[:teardown.index("self.ws = None")]
        self.assertIn("close_quietly(self.mic)", teardown)
        self.assertNotIn("mic.stop()", re.sub(r"self\.mic", "", teardown))


if __name__ == "__main__":
    unittest.main()
